import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from huaji_port.configuration import normalize
from huaji_port.push_rules import (
    PushRulesPanel,
    RulesConflict,
    build_candidate,
    read_rules,
)
from huaji_port.runtime import Runtime

ORIGIN = "bot:GroupMessage:allowed"


class Config(dict):
    def __init__(self, path):
        super().__init__(
            allowed_sessions=[ORIGIN],
            tickets_enabled=True,
            extensions_json=json.dumps(
                {
                    "github": {
                        "address": "wss://private.example/mq",
                        "token": "SECRET_MQ",
                        "short_link": {
                            "token": "SECRET_LINK",
                            "access_client_secret": "SECRET_ACCESS",
                        },
                    },
                    "repair": {
                        "since": "2026-01-01T00:00:00+08:00",
                        "nsq": {"secret": "SECRET_NSQ"},
                    },
                    "push_api": {"token": "SECRET_PUSH"},
                    "minecraft_clients": [
                        {
                            "address": "wss://private.example/game",
                            "token": "SECRET_GAME",
                            "groups": [{"session": ORIGIN, "disabled_events": []}],
                        }
                    ],
                }
            ),
        )
        self.path = path
        self.fail = False
        self.saves = 0
        self.path.write_text(json.dumps(self))

    def save_config(self):
        if self.fail:
            raise OSError("simulated disk failure")
        self.path.write_text(json.dumps(self))
        self.saves += 1


def setup(tmp_path):
    raw = Config(tmp_path / "config.json")
    normalized = normalize(raw)
    services = SimpleNamespace(http=None, config=normalized)
    runtime = Runtime(normalized, services, tmp_path, AsyncMock(), None, lambda _: None)
    # Match the real callback's captured dictionary identity.
    runtime.bridges = [
        SimpleNamespace(config=normalized["minecraft_clients"][0], socket=None)
    ]
    plugin = SimpleNamespace(config=normalized, runtime=runtime, services=services)
    return raw, plugin, PushRulesPanel(plugin, raw)


def test_public_snapshot_excludes_credentials_and_connection_addresses(tmp_path):
    raw, plugin, panel = setup(tmp_path)
    snapshot = panel.snapshot()
    serialized = json.dumps(snapshot)
    assert "SECRET" not in serialized and "private.example" not in serialized
    assert snapshot["allowed_sessions"] == [ORIGIN]
    assert set(snapshot["rules"]) == {
        "github",
        "calendar",
        "repair",
        "bridge",
        "summary",
    }
    plugin.runtime.store.close()


@pytest.mark.asyncio
async def test_save_updates_running_references_and_preserves_hidden_settings(tmp_path):
    raw, plugin, panel = setup(tmp_path)
    initial = panel.snapshot()
    rules = initial["rules"]
    rules["github"] = {"groups": {"club": [ORIGIN]}, "routes": {"nbtca/repo": "club"}}
    rules["calendar"]["groups"] = [
        {"session": ORIGIN, "mode": "whitelist", "keywords": ["维修"]}
    ]
    rules["summary"].update(enabled=True, sessions=[ORIGIN], hour=9)
    rules["repair"]["threshold_days"]["open"] = 1.5
    rules["bridge"]["clients"][0]["groups"][0]["to_client"] = False
    old_client = plugin.runtime.bridges[0].config
    result = await panel.save({"revision": initial["revision"], "rules": rules})
    assert raw.saves == 1 and result["revision"] != initial["revision"]
    assert plugin.services.config is plugin.config is plugin.runtime.config
    assert plugin.runtime.summary.config is plugin.config["summary"]
    assert plugin.runtime.github_presentation.config is plugin.config["github"]
    assert (
        plugin.runtime.bridges[0].config
        is old_client
        is plugin.config["minecraft_clients"][0]
    )
    assert not old_client["groups"][0]["to_client"]
    assert plugin.config["github"]["routes"] == {"nbtca/repo": "club"}
    saved = normalize(json.loads(raw.path.read_text()))
    assert saved["github"]["token"] == "SECRET_MQ"
    assert saved["github"]["short_link"]["access_client_secret"] == "SECRET_ACCESS"
    assert saved["repair"]["nsq"]["secret"] == "SECRET_NSQ"
    assert saved["repair"]["threshold_days"]["open"] == 1.5
    assert saved["push_api"]["token"] == "SECRET_PUSH"
    await plugin.runtime.close()


@pytest.mark.asyncio
async def test_disk_failure_restores_config_and_live_state(tmp_path):
    raw, plugin, panel = setup(tmp_path)
    initial = panel.snapshot()
    before = raw.path.read_text()
    original = copy.deepcopy(plugin.config)
    rules = initial["rules"]
    rules["calendar"]["enabled"] = False
    raw.fail = True
    with pytest.raises(OSError):
        await panel.save({"revision": initial["revision"], "rules": rules})
    assert raw.path.read_text() == before
    assert dict(raw) == json.loads(before) and plugin.config == original
    await plugin.runtime.close()


@pytest.mark.asyncio
async def test_stale_page_or_command_change_returns_conflict(tmp_path):
    raw, plugin, panel = setup(tmp_path)
    first = panel.snapshot()
    await panel.save({"revision": first["revision"], "rules": first["rules"]})
    with pytest.raises(RulesConflict):
        await panel.save({"revision": first["revision"], "rules": first["rules"]})
    second = panel.snapshot()
    plugin.runtime.store.set("mc-events:" + ORIGIN, ["Chat"])
    with pytest.raises(RulesConflict):
        await panel.save({"revision": second["revision"], "rules": second["rules"]})
    await plugin.runtime.close()


@pytest.mark.asyncio
async def test_panel_event_changes_override_legacy_command_and_survive_reload(tmp_path):
    raw, plugin, panel = setup(tmp_path)
    plugin.runtime.store.set("mc-events:" + ORIGIN, ["Chat"])
    initial = panel.snapshot()
    assert initial["rules"]["bridge"]["clients"][0]["groups"][0]["disabled_events"] == [
        "Chat"
    ]
    initial["rules"]["bridge"]["clients"][0]["groups"][0]["disabled_events"] = []
    await panel.save({"revision": initial["revision"], "rules": initial["rules"]})
    rule = plugin.config["minecraft_clients"][0]["groups"][0]
    assert plugin.runtime.disabled_events(rule) == [] and rule.get("events_revision")
    saved_rule = normalize(json.loads(raw.path.read_text()))["minecraft_clients"][0][
        "groups"
    ][0]
    assert plugin.runtime.disabled_events(saved_rule) == []
    plugin.runtime.store.set(plugin.runtime.event_key(rule), ["PlayerDeath"])
    assert panel.snapshot()["rules"]["bridge"]["clients"][0]["groups"][0][
        "disabled_events"
    ] == ["PlayerDeath"]
    await plugin.runtime.close()


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["github"].update(groups={"bad group": []}),
        lambda r: r["github"].update(routes={"org/repo": "missing"}),
        lambda r: r["github"].update(groups={"default": ["bot:GroupMessage:unknown"]}),
        lambda r: r["github"].update(
            routes={"org/repo": "default", "ORG/repo": "default"}
        ),
        lambda r: r["calendar"]["club"].update(weekly_hour=24),
        lambda r: r["calendar"].update(
            groups=[{"session": ORIGIN, "mode": "all", "keywords": [""]}]
        ),
        lambda r: r["repair"]["threshold_days"].update(open=float("nan")),
        lambda r: r["repair"].update(since="2026-11-12T09:00:00"),
        lambda r: r["repair"].update(since=""),
        lambda r: r["summary"].update(minute=True),
        lambda r: r["summary"].update(sessions=[ORIGIN, ORIGIN]),
        lambda r: r["summary"].update(system_prompt="overwrite secret"),
        lambda r: r["bridge"]["clients"][0].update(token="overwrite token"),
        lambda r: r["bridge"]["clients"][0]["groups"][0].update(
            disabled_events=["unknown"]
        ),
    ],
)
def test_invalid_rules_are_rejected_before_config_changes(tmp_path, change):
    raw, plugin, panel = setup(tmp_path)
    before = copy.deepcopy(dict(raw))
    rules = panel.snapshot()["rules"]
    change(rules)
    with pytest.raises((ValueError, TypeError)):
        build_candidate(raw, rules, plugin.runtime)
    assert dict(raw) == before and raw.saves == 0
    plugin.runtime.store.close()


def test_unchanged_save_preserves_event_overrides(tmp_path):
    raw, plugin, panel = setup(tmp_path)
    plugin.runtime.store.set("mc-events:" + ORIGIN, ["Chat"])
    rules = read_rules(plugin.config, plugin.runtime)
    _, candidate = build_candidate(raw, rules, plugin.runtime)
    rule = candidate["minecraft_clients"][0]["groups"][0]
    assert not rule.get("events_revision")
    assert plugin.runtime.disabled_events(rule) == ["Chat"]
    plugin.runtime.store.close()
