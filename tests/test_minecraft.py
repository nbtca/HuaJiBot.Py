import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from huaji_port.qq_delivery import QQGroupDelivery
from huaji_port.runtime import Runtime
from huaji_port.services import Services

ORIGIN = "bot:GroupMessage:group"
CONFIG = {"allowed_sessions": [ORIGIN]}
CLIENT = {"address": "wss://example.invalid", "groups": [{"session": ORIGIN}]}
SERVERS = [
    {"name": "生电服", "address": "mc.example.com:25565"},
    {"name": "GTO", "address": "mc.example.com:25566"},
]


@pytest.mark.asyncio
async def test_query_distinguishes_instances_and_survives_one_offline(monkeypatch):
    server = SimpleNamespace(
        async_status=AsyncMock(
            return_value=SimpleNamespace(
                players=SimpleNamespace(
                    online=3,
                    max=20,
                    sample=[SimpleNamespace(name="Alice", id="sample-id")],
                ),
                version=SimpleNamespace(name="1.20.1"),
                latency=25.5,
            )
        )
    )
    lookup = AsyncMock(side_effect=[ConnectionError(), server])
    monkeypatch.setattr("huaji_port.services.JavaServer.async_lookup", lookup)
    services = Services({"minecraft_enabled": True, "minecraft_servers": SERVERS})
    result = await services.minecraft()
    assert "生电服" in result
    assert "状态查询失败" in result
    assert "GTO：3 人在线" in result
    assert "mc.example.com" not in result
    assert "Alice" in result and "sample-id" not in result


@pytest.mark.asyncio
async def test_single_address_query_compact_output(monkeypatch):
    server = SimpleNamespace(
        async_status=AsyncMock(
            return_value=SimpleNamespace(
                players=SimpleNamespace(online=2, max=10),
                version=SimpleNamespace(name="1.21.1"),
                latency=12,
            )
        )
    )
    monkeypatch.setattr(
        "huaji_port.services.JavaServer.async_lookup", AsyncMock(return_value=server)
    )
    services = Services(
        {"minecraft_enabled": True, "minecraft_address": "mc.example.com:25565"}
    )
    assert await services.minecraft() == ("Minecraft：2 人在线")


@pytest.mark.asyncio
async def test_player_snapshot_and_expiry_when_status_offline(monkeypatch, tmp_path):
    path = tmp_path / "players.json"
    monkeypatch.setattr(
        "huaji_port.services.JavaServer.async_lookup",
        AsyncMock(side_effect=ConnectionError),
    )
    services = Services(
        {
            "minecraft_enabled": True,
            "minecraft_servers": [{**SERVERS[1], "player_snapshot": str(path)}],
        }
    )
    report = {
        "timestamp": int(time.time()),
        "players": [
            {"name": "Alice", "uuid": "player-id", "ping": 0, "world": "gt:moon"}
        ],
    }
    path.write_text(json.dumps(report))
    result = await services.minecraft()
    assert "Alice · 0 ms · gt:moon" in result and "player-id" not in result
    report["timestamp"] -= 60
    path.write_text(json.dumps(report))
    result = await services.minecraft()
    assert "快照" in result and "Alice" not in result


@pytest.mark.asyncio
async def test_both_queries_keep_bridge_details_and_send_only_to_bridge(
    tmp_path, monkeypatch
):
    server = SimpleNamespace(
        async_status=AsyncMock(
            return_value=SimpleNamespace(players=SimpleNamespace(online=0))
        )
    )
    monkeypatch.setattr(
        "huaji_port.services.JavaServer.async_lookup", AsyncMock(return_value=server)
    )
    cfg = {
        **CONFIG,
        "minecraft_enabled": True,
        "minecraft_servers": SERVERS,
    }
    services = Services(cfg)
    bot = Runtime(cfg, services, tmp_path, AsyncMock(), None, lambda _: None)
    bridge = SimpleNamespace(
        config={**CLIENT, "name": "生电服"},
        query=AsyncMock(
            return_value=[
                {
                    "source": {"display_name": "Server"},
                    "data": {
                        "players": [
                            {
                                "name": "Alice",
                                "uuid": "player-id",
                                "ping": 12,
                                "world": "world",
                            }
                        ]
                    },
                }
            ]
        ),
        send=AsyncMock(),
    )
    bot.bridges = [bridge]
    try:
        result = await bot.players(ORIGIN)
        assert "生电服：1 人在线" in result
        assert "GTO：0 人在线" in result
        assert result.count("生电服") == 1
        assert "Alice · 12 ms · world" in result and "player-id" not in result
        bridge.query.return_value = []
        assert "玩家查询超时" in await bot.players(ORIGIN)
        bridge.query.return_value = [{"data": {"players": []}}]
        assert await bot.players(ORIGIN) == "生电服：0 人在线\nGTO：0 人在线"
        await bot.observe(ORIGIN, "user", "User", "hello", "message", 0)
        bridge.send.assert_awaited_once()
        assert bridge.send.await_args.args[0]["data"]["message"] == "hello"
    finally:
        await bot.close()


def event(kind, server="server"):
    return {
        "type": kind,
        "source": {"name": "Minecraft", "display_name": server},
        "data": {"player_name": "Player"},
    }


def runtime(path, receipt=None, recall=None):
    return Runtime(
        CONFIG,
        None,
        path,
        AsyncMock(return_value=True),
        None,
        lambda _: None,
        send_receipt=receipt or AsyncMock(return_value=["join-id"]),
        recall=recall or AsyncMock(),
    )


@pytest.mark.asyncio
async def test_join_receipt_survives_restart_and_quit_recalls_only_join(tmp_path):
    bot = runtime(tmp_path)
    await bot.minecraft_event(CLIENT, event("player_join"))
    await bot.delivery_once()
    bot.send_receipt.assert_awaited_once()
    await bot.close()
    bot = runtime(tmp_path)
    await bot.minecraft_event(CLIENT, event("player_quit"))
    await bot.delivery_once()
    bot.recall.assert_awaited_once_with(ORIGIN, "join-id")
    bot.send.assert_not_awaited()
    await bot.delivery_once()
    assert "离开了服务器" in bot.send.await_args.args[1]
    assert bot.store.counts() == {"sent": 3}
    await bot.close()


@pytest.mark.asyncio
async def test_player_leaves_before_join_is_sent(tmp_path):
    bot = runtime(tmp_path)
    await bot.minecraft_event(CLIENT, event("player_join"))
    await bot.minecraft_event(CLIENT, event("player_quit"))
    for _ in range(3):
        await bot.delivery_once()
    bot.send_receipt.assert_not_awaited()
    bot.recall.assert_not_awaited()
    assert bot.send.await_count == 1 and "离开了" in bot.send.await_args.args[1]
    assert bot.store.counts() == {"cancelled": 1, "sent": 2}
    await bot.close()


@pytest.mark.asyncio
async def test_quit_during_qq_send_keeps_receipt_for_recall(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def send(origin, text):
        started.set()
        await release.wait()
        return ["delayed-id"]

    bot = runtime(tmp_path, receipt=send)
    await bot.minecraft_event(CLIENT, event("player_join"))
    task = asyncio.create_task(bot.delivery_once())
    await started.wait()
    await bot.minecraft_event(CLIENT, event("player_quit"))
    release.set()
    await task
    await bot.delivery_once()
    bot.recall.assert_awaited_once_with(ORIGIN, "delayed-id")
    await bot.close()


@pytest.mark.asyncio
async def test_same_player_on_two_servers_and_rapid_rejoin_are_isolated(tmp_path):
    bot = runtime(
        tmp_path,
        receipt=AsyncMock(side_effect=[["server-a"], ["server-b"], ["rejoin-a"]]),
    )
    await bot.minecraft_event(CLIENT, event("player_join", "A"))
    await bot.minecraft_event(CLIENT, event("player_join", "B"))
    await bot.delivery_once()
    await bot.delivery_once()
    await bot.minecraft_event(CLIENT, event("player_quit", "A"))
    await bot.minecraft_event(CLIENT, event("player_join", "A"))
    await bot.delivery_once()
    bot.recall.assert_awaited_once_with(ORIGIN, "server-a")
    await bot.delivery_once()
    await bot.delivery_once()
    await bot.minecraft_event(CLIENT, event("player_quit", "B"))
    await bot.delivery_once()
    assert [call.args[1] for call in bot.recall.await_args_list] == [
        "server-a",
        "server-b",
    ]
    await bot.close()


@pytest.mark.asyncio
async def test_partial_recall_retries_only_remaining_ids(tmp_path):
    bot = runtime(
        tmp_path,
        receipt=AsyncMock(return_value=["one", "two"]),
        recall=AsyncMock(side_effect=[None, ConnectionError(), None]),
    )
    await bot.minecraft_event(CLIENT, event("player_join"))
    await bot.delivery_once()
    await bot.minecraft_event(CLIENT, event("player_quit"))
    await bot.delivery_once()
    await bot.delivery_once()
    bot.store.db.execute("UPDATE queue SET due=0 WHERE status='pending'")
    bot.store.db.commit()
    await bot.delivery_once()
    assert [call.args[1] for call in bot.recall.await_args_list] == [
        "one",
        "two",
        "two",
    ]
    assert bot.store.counts() == {"sent": 3}
    await bot.close()


@pytest.mark.asyncio
async def test_disabled_join_event_and_unapproved_group_do_not_send(tmp_path):
    bot = runtime(tmp_path)
    cfg = {
        "address": CLIENT["address"],
        "groups": [
            {"session": ORIGIN, "disabled_events": ["JoinLeft"]},
            {"session": "other:GroupMessage:group"},
        ],
    }
    await bot.minecraft_event(cfg, event("player_join"))
    await bot.minecraft_event(cfg, event("player_quit"))
    assert bot.store.counts() == {}
    await bot.close()


@pytest.mark.asyncio
async def test_duplicate_join_while_online_does_not_duplicate_notices(tmp_path):
    bot = runtime(tmp_path)
    await bot.minecraft_event(CLIENT, event("player_join"))
    await bot.minecraft_event(CLIENT, event("player_join"))
    assert bot.store.counts() == {"pending": 1}
    await bot.close()


@pytest.mark.asyncio
async def test_official_group_receipt_uses_the_current_adapter():
    api = SimpleNamespace(post_group_message=AsyncMock(return_value={"id": "real-id"}))
    platform = SimpleNamespace(
        meta=lambda: SimpleNamespace(name="qq_official"),
        client=SimpleNamespace(api=api),
    )
    delivery = QQGroupDelivery(SimpleNamespace(get_platform_inst=lambda _: platform))
    assert await delivery.send(ORIGIN, "joined") == ["real-id"]
    api.post_group_message.assert_awaited_once_with(
        group_openid="group", msg_type=0, content="joined"
    )
    api.post_group_message.return_value = {}
    with pytest.raises(ValueError):
        await delivery.send(ORIGIN, "joined")
    with pytest.raises(ValueError):
        delivery.resolve("bot:FriendMessage:user")
