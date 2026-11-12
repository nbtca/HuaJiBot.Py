import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "converter", Path(__file__).resolve().parents[1] / "tools/convert_config.py"
)
converter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(converter)


def test_mapping_preserves_protocol_routing_and_requires_explicit_ids():
    source = {
        "Calendar": {
            "ReminderGroups": [
                {"GroupId": "123", "Mode": "WhiteList", "Keywords": ["club"]}
            ]
        },
        "MessageBridge": {
            "Clients": [
                {
                    "Address": "wss://example.invalid",
                    "Token": "test-secret",
                    "Groups": [{"GroupId": "123", "ForwardToClient": False}],
                }
            ]
        },
        "RepairTeam": {
            "RemindSince": "2026-01-01T00:00:00+08:00",
            "RemindGroups": ["123"],
        },
    }
    with pytest.raises(ValueError):
        converter.convert(source, {})
    cfg = converter.convert(source, {"123": "bot:GroupMessage:opaque"})
    extra = json.loads(cfg["extensions_json"])
    assert cfg["allowed_sessions"] == ["bot:GroupMessage:opaque"]
    rule = extra["minecraft_clients"][0]["groups"][0]
    assert rule["legacy_group_id"] == "123" and not rule["to_client"]
    assert extra["minecraft_clients"][0]["token"] == "test-secret"
    assert extra["calendar_reminders"][0]["mode"] == "whitelist"
    assert cfg["admin_ids"] == []
