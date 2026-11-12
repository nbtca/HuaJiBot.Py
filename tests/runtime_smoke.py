"""Run with AstrBot's Python and its app directory on sys.path; never sends QQ messages."""

import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


class MemoryConfig(dict):
    saves = 0

    def save_config(self):
        self.saves += 1


async def check():
    plugin_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "huaji_port",
        plugin_root / "__init__.py",
        submodule_search_locations=[str(plugin_root)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules["huaji_port"] = package
    spec.loader.exec_module(package)
    module = importlib.import_module("huaji_port.main")
    context = SimpleNamespace(
        register_web_api=Mock(),
        send_message=AsyncMock(return_value=True),
        get_current_chat_provider_id=AsyncMock(return_value="test-provider"),
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(completion_text="test answer")
        ),
    )
    config = MemoryConfig(
        {
            "allowed_sessions": ["bot:GroupMessage:test"],
            "admin_ids": ["admin"],
            "cooldown_seconds": 0,
        }
    )
    plugin = module.HuajiPlugin(context, config)
    assert context.register_web_api.call_count == 2

    def event(text, message_id, origin="bot:GroupMessage:test", sender="user"):
        return SimpleNamespace(
            message_str=text,
            unified_msg_origin=origin,
            message_obj=SimpleNamespace(message_id=message_id),
            get_platform_name=lambda: "qq_official",
            get_sender_id=lambda: sender,
            get_self_id=lambda: "bot",
            should_call_llm=lambda value: None,
            stop_event=lambda: None,
            plain_result=lambda text: text,
            send=AsyncMock(),
        )

    temp = tempfile.TemporaryDirectory()
    with patch.object(module.StarTools, "get_data_dir", return_value=Path(temp.name)):
        await plugin.initialize()
    try:
        e = event("帮助", "a")
        await plugin.on_command(e)
        assert e.send.await_count == 1
        context.llm_generate.assert_not_awaited()
        await plugin.on_command(e)
        assert e.send.await_count == 1
        e = event("帮助", "b", origin="other:GroupMessage:test")
        await plugin.on_command(e)
        e.send.assert_not_awaited()
        e = event("公告 test", "c")
        await plugin.on_command(e)
        assert "管理员" in e.send.await_args.args[0]
        e = event("问 hello", "d")
        await plugin.on_command(e)
        context.llm_generate.assert_awaited_once()
        assert e.send.await_args.args[0] == "test answer"
        e = event("普通聊天提到日程", "e")
        await plugin.on_command(e)
        e.send.assert_not_awaited()
        assert context.llm_generate.await_count == 1
        from PIL import Image

        image = plugin.runtime.card_directory / "smoke.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16)).save(image)
        plugin.runtime.enqueue(
            "github:smoke",
            "bot:GroupMessage:test",
            "GitHub test",
            payload={"image": str(image)},
        )
        await plugin.runtime.delivery_once()
        context.send_message.assert_awaited_once()
        chain = context.send_message.await_args.args[1]
        assert chain.chain[0].type == "Image"
        assert any(component.type == "Image" for component in chain.chain)
        assert any(component.type == "Plain" for component in chain.chain)
        import astrbot.api.web as web

        with patch.object(web, "request", SimpleNamespace(username=None)):
            assert (await plugin.panel_read()).status_code == 401
            assert (await plugin.panel_save()).status_code == 401
        with patch.object(web, "request", SimpleNamespace(username="admin")):
            response = await plugin.panel_read()
            assert response.status_code == 200
            snapshot = json.loads(response.body)
        payload = {"revision": snapshot["revision"], "rules": snapshot["rules"]}
        request = SimpleNamespace(
            username="admin", body=AsyncMock(return_value=json.dumps(payload).encode())
        )
        with patch.object(web, "request", request):
            assert (await plugin.panel_save()).status_code == 200
            assert config.saves == 1
            assert (await plugin.panel_save()).status_code == 409
        request.body.return_value = b"invalid json"
        with patch.object(web, "request", request):
            assert (await plugin.panel_save()).status_code == 400
        request.body.return_value = b"x" * (256 * 1024 + 1)
        with patch.object(web, "request", request):
            assert (await plugin.panel_save()).status_code == 413
        from huaji_port.qq_delivery import QQGroupDelivery

        api = SimpleNamespace(
            post_group_message=AsyncMock(return_value={"id": "join/test"}),
            _http=SimpleNamespace(request=AsyncMock(return_value=None)),
        )
        adapter = SimpleNamespace(
            meta=lambda: SimpleNamespace(name="qq_official"),
            client=SimpleNamespace(api=api),
        )
        delivery = QQGroupDelivery(SimpleNamespace(get_platform_inst=lambda _: adapter))
        assert await delivery.send("bot:GroupMessage:group", "joined") == ["join/test"]
        await delivery.recall("bot:GroupMessage:group", "join/test")
        route = api._http.request.await_args.args[0]
        assert (
            route.method == "DELETE" and route.parameters["message_id"] == "join%2Ftest"
        )
        assert route.parameters["group_openid"] == "group"
    finally:
        await plugin.terminate()
        temp.cleanup()
    print(
        "AstrBot runtime smoke: 14 checks passed; no real network/model/message calls."
    )


if __name__ == "__main__":
    asyncio.run(check())
