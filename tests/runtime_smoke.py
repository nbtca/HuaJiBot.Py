"""Run with AstrBot's Python and its app directory on sys.path; never sends QQ messages."""

import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


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
        send_message=AsyncMock(return_value=True),
        get_current_chat_provider_id=AsyncMock(return_value="test-provider"),
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(completion_text="test answer")
        ),
    )
    config = {
        "allowed_sessions": ["bot:GroupMessage:test"],
        "admin_ids": ["admin"],
        "cooldown_seconds": 0,
    }
    plugin = module.HuajiPlugin(context, config)

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
    finally:
        await plugin.terminate()
        temp.cleanup()
    print(
        "AstrBot runtime smoke: 6 checks passed; no real network/model/message calls."
    )


if __name__ == "__main__":
    asyncio.run(check())
