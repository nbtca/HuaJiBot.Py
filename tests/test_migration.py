import asyncio
import hashlib
import hmac
import json
import struct
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from huaji_port.business import (
    active_tickets,
    calendar_notifications,
    github_targets,
    stalled,
)
from huaji_port.configuration import normalize
from huaji_port.runtime import Runtime
from huaji_port.services import TZ, expand_calendar
from huaji_port.state import State
from huaji_port.summaries import Summaries
from huaji_port.transports import MQClient, NSQConsumer, read_nsq_frame
from icalendar import Calendar

ORIGIN = "test:GroupMessage:group"
NOW = datetime(2026, 11, 5, 12, tzinfo=TZ)


def config(extra=None):
    return normalize(
        {
            "allowed_sessions": [ORIGIN],
            "tickets_enabled": False,
            "extensions_json": json.dumps(extra or {}),
        }
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"summary": {"sessions": ["other:GroupMessage:group"]}},
        {"summary": {"hour": 24}},
        {"summary": {"max_prompt_chars": 128}},
        {"github": {"routes": {"org/repo": "missing"}}},
        {"minecraft_clients": [{"address": "http://invalid", "groups": []}]},
        {"unknown": True},
        {"summary": {"enabled": "yes"}},
    ],
)
def test_config_rejects_unsafe_or_invalid(extra):
    with pytest.raises(ValueError):
        config(extra)


@pytest.mark.asyncio
async def test_tickets_paginate_details_and_stalled():
    calls = []

    async def fetch(url):
        calls.append(url)
        if "status=" in url:
            if "status=open" in url and "offset=0" in url:
                return [
                    {"eventId": i, "gmtCreate": "2026-11-01T00:00:00+08:00"}
                    for i in range(50)
                ]
            return []
        return {
            "eventId": int(url.rsplit("/", 1)[1]),
            "status": "open",
            "gmtCreate": "2026-11-01T00:00:00+08:00",
            "logs": [{"gmtCreate": "2026-11-04T23:00:00+08:00"}],
        }

    tickets = await active_tickets(
        fetch, "https://example.invalid", NOW - timedelta(days=30)
    )
    assert len(tickets) == 50
    assert any("offset=50" in url for url in calls)
    assert stalled(tickets, NOW, {}) == []
    assert len(stalled(tickets, NOW + timedelta(days=2), {})) == 50


@pytest.mark.asyncio
async def test_ticket_detail_failure_is_not_silent_partial():
    async def fetch(url):
        if "status=" in url:
            return [{"eventId": 1, "gmtCreate": NOW.isoformat()}]
        raise ConnectionError()

    with pytest.raises(ConnectionError):
        await active_tickets(fetch, "https://example.invalid", NOW - timedelta(days=1))


def test_private_repo_needs_explicit_route():
    cfg = {"groups": {"default": [ORIGIN]}, "routes": {}}
    body = {"repository": {"full_name": "org/private", "private": True}}
    assert github_targets(body, cfg) == []
    cfg["routes"]["org/private"] = "default"
    assert github_targets(body, cfg) == [ORIGIN]


@pytest.mark.asyncio
async def test_outbox_retry_does_not_block_other_target_and_survives_restart(tmp_path):
    send = AsyncMock(side_effect=ConnectionError())
    runtime = Runtime(config(), None, tmp_path, send, None, lambda _: None)
    runtime.enqueue("one", ORIGIN, "hello")
    runtime.enqueue("two", ORIGIN, "next")
    assert not runtime.enqueue("no", "other", "secret")
    await runtime.delivery_once()
    assert runtime.store.next()[0] == "two:0"
    send.side_effect = None
    send.return_value = True
    await runtime.delivery_once()
    assert runtime.store.counts() == {"pending": 1, "sent": 1}
    await runtime.close()
    runtime = Runtime(config(), None, tmp_path, send, None, lambda _: None)
    assert runtime.enqueue("two", ORIGIN, "changed")
    assert runtime.store.counts() == {"pending": 1, "sent": 1}
    await runtime.close()


def test_calendar_repeat_skip_all_day_and_filters():
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:normal
DTSTART:20261105T050000Z
DTEND:20261105T060000Z
SUMMARY:club repair
RRULE:FREQ=DAILY;COUNT=2
END:VEVENT
BEGIN:VEVENT
UID:skip
DTSTART:20261105T050000Z
DESCRIPTION:[SKIP NOTIFICATION]
SUMMARY:club hidden
END:VEVENT
BEGIN:VEVENT
UID:day
DTSTART;VALUE=DATE:20261105
SUMMARY:club all-day
END:VEVENT
END:VCALENDAR"""
    events = expand_calendar(
        Calendar.from_ical(raw), NOW - timedelta(days=1), NOW + timedelta(days=8)
    )
    rules = [{"session": ORIGIN, "mode": "whitelist", "keywords": ["club"]}]
    jobs = list(calendar_notifications(events, rules, NOW, {}))
    assert len(jobs) == 1 and ":normal:" in jobs[0][0] and jobs[0][3] == NOW.timestamp()
    tomorrow_jobs = list(
        calendar_notifications(events, rules, NOW, {"daily": True, "daily_hour": 12})
    )
    assert any("club:daily:" in j[0] and "repair" in j[2] for j in tomorrow_jobs)


@pytest.mark.asyncio
async def test_history_scope_midnight_summary_and_cleanup(tmp_path):
    store = State(tmp_path / "s.db")
    start = NOW.replace(hour=0)
    store.record(
        ORIGIN, "before", (start - timedelta(seconds=1)).timestamp(), "u", "yesterday"
    )
    store.record(ORIGIN, "today", start.timestamp(), "u", "today")
    store.record("other", "today", start.timestamp(), "u", "private")
    llm = AsyncMock(return_value="summary")
    summary = Summaries(store, {"min_messages": 1, "max_prompt_chars": 512}, llm)
    await summary.generate(ORIGIN, start.date())
    prompt = llm.await_args.args[1]
    assert "today" in prompt and "yesterday" not in prompt and "private" not in prompt
    store.prune(start.timestamp())
    assert len(store.history(ORIGIN, 0, NOW.timestamp())) == 1
    store.close()


@pytest.mark.asyncio
async def test_summary_failure_attempts_are_bounded_and_durable(tmp_path):
    cfg = config(
        {
            "summary": {
                "enabled": True,
                "scheduled": True,
                "sessions": [ORIGIN],
                "hour": 12,
                "max_retries": 2,
            }
        }
    )
    runtime = Runtime(cfg, None, tmp_path, None, None, lambda _: None)
    runtime.summary.generate = AsyncMock(side_effect=ConnectionError())
    for _ in range(4):
        await runtime.summary_tick(NOW)
    assert runtime.summary.generate.await_count == 2
    await runtime.close()
    runtime = Runtime(cfg, None, tmp_path, None, None, lambda _: None)
    runtime.summary.generate = AsyncMock()
    await runtime.summary_tick(NOW)
    runtime.summary.generate.assert_not_awaited()
    await runtime.close()


@pytest.mark.asyncio
async def test_mq_wire_query_and_response_correlation():
    received = []

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer test"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            value = json.loads(message.data)
            received.append(value)
            if value["type"] == "get_player_list_request":
                await ws.send_json(
                    {
                        "type": "get_player_list_response",
                        "source": {"display_name": "MC"},
                        "data": {
                            "request_id": value["data"]["request_id"],
                            "players": [{"name": "p", "ping": 10, "world": "world"}],
                        },
                    }
                )
        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    async with aiohttp.ClientSession() as http:
        client = MQClient(
            http,
            {"address": f"http://127.0.0.1:{port}/", "token": "test"},
            AsyncMock(),
            lambda _: None,
        )
        task = asyncio.create_task(client.run())
        try:
            for _ in range(100):
                if client.socket:
                    break
                await asyncio.sleep(0.01)
            result = await client.query(0.1)
            assert result[0]["data"]["players"][0]["name"] == "p"
            assert received[0]["source"]["name"] == "HuaJiBot.Py.MessageBridge"
            assert client.pending == {}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await runner.cleanup()


@pytest.mark.asyncio
async def test_nsq_frame_bounds():
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack(">I", 3))
    with pytest.raises(ValueError):
        await read_nsq_frame(reader)


@pytest.mark.asyncio
async def test_nsq_fin_after_persist_and_heartbeat():
    done = asyncio.Event()
    commands = []
    receive = AsyncMock()

    async def serve(reader, writer):
        async def frame(kind, payload):
            writer.write(struct.pack(">II", len(payload) + 4, kind) + payload)
            await writer.drain()

        try:
            assert await reader.readexactly(4) == b"  V2"
            assert await reader.readline() == b"IDENTIFY\n"
            n = struct.unpack(">I", await reader.readexactly(4))[0]
            await reader.readexactly(n)
            await frame(0, b'{"auth_required":false}')
            assert await reader.readline() == b"SUB topic channel\n"
            await frame(0, b"OK")
            assert await reader.readline() == b"RDY 1\n"
            await frame(0, b"_heartbeat_")
            commands.append(await reader.readline())
            await frame(
                2,
                struct.pack(">QH", 0, 1) + b"0123456789abcdef" + b'{"action":"create"}',
            )
            commands.append(await reader.readline())
            assert receive.await_count == 1
            assert await reader.readline() == b"RDY 1\n"
            await frame(
                2,
                struct.pack(">QH", 0, 1) + b"fedcba9876543210" + b'{"action":"close"}',
            )
            assert await reader.readline() == b"FIN fedcba9876543210\n"
            assert await reader.readline() == b"RDY 1\n"
            assert receive.await_count == 2
        finally:
            writer.close()
            await writer.wait_closed()
            done.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    consumer = NSQConsumer(
        {"address": f"127.0.0.1:{port}", "topic": "topic", "channel": "channel"},
        receive,
        lambda _: None,
    )
    task = asyncio.create_task(consumer.consume())
    try:
        await asyncio.wait_for(done.wait(), 3)
        assert commands == [b"NOP\n", b"FIN 0123456789abcdef\n"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_http_push_auth_scope_and_github_signature(tmp_path):
    cfg = config(
        {
            "push_api": {
                "enabled": True,
                "port": 0,
                "token": "t" * 24,
                "github_secret": "s" * 24,
            },
            "github": {"enabled": True, "groups": {"default": [ORIGIN]}},
        }
    )
    runtime = Runtime(cfg, None, tmp_path, None, None, lambda _: None)
    await runtime.start_http()
    site = next(iter(runtime.runner.sites))
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    async with aiohttp.ClientSession() as client:
        async with client.post(
            base + "/push", json={"id": "1", "session": ORIGIN, "text": "hello"}
        ) as response:
            assert response.status == 401
        headers = {"Authorization": "Bearer " + "t" * 24}
        async with client.post(
            base + "/push",
            headers=headers,
            json={"id": "1", "session": "other", "text": "hello"},
        ) as response:
            assert response.status == 403
        for _ in range(2):
            async with client.post(
                base + "/push",
                headers=headers,
                json={"id": "1", "session": ORIGIN, "text": "hello"},
            ) as response:
                assert response.status == 202
        body = json.dumps(
            {
                "repository": {"full_name": "org/repo", "default_branch": "main"},
                "sender": {"login": "user", "type": "User"},
                "ref": "refs/heads/main",
                "commits": [
                    {"id": "abcdef123", "message": "Update", "modified": ["src/app.py"]}
                ],
            }
        ).encode()
        sig = "sha256=" + hmac.new(b"s" * 24, body, hashlib.sha256).hexdigest()
        async with client.post(
            base + "/github",
            data=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "delivery",
            },
        ) as response:
            assert response.status == 202
        assert runtime.store.counts() == {"pending": 2}
    await runtime.close()


@pytest.mark.asyncio
async def test_bridge_scope_filters_and_observer_do_not_use_llm(tmp_path):
    cfg = config(
        {
            "minecraft_clients": [
                {
                    "address": "wss://example.invalid",
                    "groups": [
                        {"session": ORIGIN, "to_client": True, "from_client": True}
                    ],
                }
            ]
        }
    )
    llm = AsyncMock()
    runtime = Runtime(cfg, None, tmp_path, AsyncMock(), llm, lambda _: None)
    client = SimpleNamespace(config=cfg["minecraft_clients"][0], send=AsyncMock())
    runtime.bridges = [client]
    await runtime.observe(ORIGIN, "u", "User", "hello", "1", NOW.timestamp())
    await runtime.observe(ORIGIN, "u", "User", "hello", "1", NOW.timestamp())
    await runtime.observe("other", "u", "User", "hello", "2", NOW.timestamp())
    await runtime.observe(
        ORIGIN, "b", "Bot", "hello", "3", NOW.timestamp(), is_bot=True
    )
    await runtime.observe(ORIGIN, "u", "User", "日程", "4", NOW.timestamp())
    client.send.assert_awaited_once()
    assert client.send.await_args.args[0]["data"]["sender_id"] == "u"
    llm.assert_not_awaited()
    runtime.store.set("mc-events:" + ORIGIN, ["Chat"])
    await runtime.minecraft_event(
        client.config,
        {"type": "player_chat", "data": {"player_name": "p", "message": "test"}},
    )
    assert runtime.store.counts() == {}
    await runtime.minecraft_event(
        client.config, {"type": "player_join", "data": {"player_name": "p"}}
    )
    assert runtime.store.counts() == {"pending": 1}
    await runtime.close()


def test_queue_multisegment_transaction_rolls_back_on_collision(tmp_path):
    state = State(tmp_path / "q.db")
    state.bind(ORIGIN)
    state.enqueue("batch:1", ORIGIN, "existing")
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        state.enqueue_parts("batch", ORIGIN, "x" * 3000, NOW.timestamp())
    assert state.db.execute("SELECT 1 FROM queue WHERE id='batch:0'").fetchone() is None
    assert state.get("accepted:batch") is None
    state.close()


def test_all_day_until_and_daylight_saving_recurrence():
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:all-day
DTSTART;VALUE=DATE:20261101
RRULE:FREQ=DAILY;UNTIL=20261103
SUMMARY:daily
END:VEVENT
BEGIN:VEVENT
UID:dst
DTSTART;TZID=America/New_York:20261031T090000
DTEND;TZID=America/New_York:20261031T100000
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:dst
END:VEVENT
END:VCALENDAR"""
    events = expand_calendar(
        Calendar.from_ical(raw),
        datetime(2026, 10, 30, tzinfo=TZ),
        datetime(2026, 11, 5, tzinfo=TZ),
    )
    assert len([e for e in events if str(e["UID"]) == "all-day"]) == 3
    dst = sorted(e.decoded("DTSTART") for e in events if str(e["UID"]) == "dst")
    assert [t.hour for t in dst] == [21, 22, 22]
