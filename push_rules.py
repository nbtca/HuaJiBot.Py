"""Public push-rule projection and validated updates for the AstrBot plugin view."""

import asyncio
import copy
import hashlib
import json
import math
import re
import uuid
from datetime import datetime

from .configuration import DEFAULT_EXTENSIONS, normalize

EVENTS = {"JoinLeft", "Chat", "PlayerDeath", "PlayerAchievement"}


class RulesConflict(ValueError):
    pass


def object_fields(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"{label}字段不完整或包含不支持的字段，请重新读取页面")
    return value


def flag(value, label):
    if type(value) is not bool:
        raise ValueError(f"{label}必须是开关值")
    return value


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label}必须为 {low} 到 {high} 之间的整数")
    return value


def text(value, limit, label):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{label}格式或长度不正确")
    return value.strip()


def items(value, label):
    if not isinstance(value, list) or len(value) > 5000:
        raise ValueError(f"{label}必须是列表，最多 5000 项")
    return value


def sessions(value, allowed, label):
    values = [text(item, 512, label) for item in items(value, label)]
    if len(values) != len(set(values)):
        raise ValueError(f"{label}包含重复目标")
    if any(value not in allowed for value in values):
        raise ValueError(
            f"{label}包含未授权会话，请先在插件设置中添加 allowed_sessions"
        )
    return values


def bridge_group(rule, runtime):
    return {
        "session": rule["session"],
        "legacy_group_id": rule.get("legacy_group_id", ""),
        "enabled": rule.get("enabled", True),
        "to_client": rule.get("to_client", True),
        "from_client": rule.get("from_client", True),
        "disabled_events": sorted(
            runtime.disabled_events(rule)
            if runtime
            else rule.get("disabled_events", [])
        ),
    }


def read_rules(config, runtime=None):
    repair = config["repair"]
    summary = config["summary"]
    return {
        "github": {
            "groups": copy.deepcopy(config["github"]["groups"]),
            "routes": copy.deepcopy(config["github"]["routes"]),
        },
        "calendar": {
            "enabled": config.get("calendar_enabled", False),
            "groups": [
                {
                    "session": rule["session"],
                    "mode": rule.get("mode", "all"),
                    "keywords": copy.deepcopy(rule.get("keywords", [])),
                }
                for rule in config["calendar_reminders"]
            ],
            "club": copy.deepcopy(config["club"]),
        },
        "repair": {
            "queries_enabled": config.get("tickets_enabled", False),
            **{
                key: copy.deepcopy(repair[key])
                for key in (
                    "since",
                    "reminder_enabled",
                    "hour",
                    "reminder_sessions",
                    "event_sessions",
                    "raw_session",
                    "threshold_days",
                )
            },
        },
        "bridge": {
            "clients": [
                {
                    "index": index,
                    "groups": [
                        bridge_group(rule, runtime) for rule in client.get("groups", [])
                    ],
                }
                for index, client in enumerate(config["minecraft_clients"])
            ]
        },
        "summary": {
            key: copy.deepcopy(summary[key])
            for key in (
                "enabled",
                "scheduled",
                "sessions",
                "hour",
                "minute",
                "min_messages",
            )
        },
    }


def build_candidate(raw_config, document, runtime=None):
    object_fields(
        document, {"github", "calendar", "repair", "bridge", "summary"}, "规则"
    )
    current = normalize(raw_config)
    allowed = set(current.get("allowed_sessions", []))
    candidate = copy.deepcopy(dict(raw_config))
    extensions = {key: copy.deepcopy(current[key]) for key in DEFAULT_EXTENSIONS}

    github = object_fields(document["github"], {"groups", "routes"}, "GitHub")
    if not isinstance(github["groups"], dict) or not isinstance(github["routes"], dict):
        raise ValueError("GitHub 分组和仓库路由必须是对象")
    groups = {}
    for name, targets in github["groups"].items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
            raise ValueError("GitHub 分组名应为 1 到 32 个字母、数字、下划线或连字符")
        groups[name] = sessions(targets, allowed, "GitHub 分组目标")
    routes = {}
    seen = set()
    for repo, group in github["routes"].items():
        if (
            not isinstance(repo, str)
            or len(repo) > 201
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
        ):
            raise ValueError("仓库名应为 owner/name")
        if repo.casefold() in seen:
            raise ValueError("GitHub 仓库重复，仓库名称不区分大小写")
        if not isinstance(group, str) or group not in groups:
            raise ValueError("GitHub 仓库指向不存在的分组")
        seen.add(repo.casefold())
        routes[repo] = group
    extensions["github"].update(groups=groups, routes=routes)

    calendar = object_fields(
        document["calendar"], {"enabled", "groups", "club"}, "日程"
    )
    candidate["calendar_enabled"] = flag(calendar["enabled"], "日程查询")
    rules = []
    for rule in items(calendar["groups"], "日程目标"):
        object_fields(rule, {"session", "mode", "keywords"}, "日程目标")
        origin = sessions([rule["session"]], allowed, "日程目标")[0]
        if origin in {row["session"] for row in rules}:
            raise ValueError("日程目标重复")
        if rule["mode"] not in {"all", "whitelist", "blacklist"}:
            raise ValueError("日程模式不正确")
        keywords = [
            text(word, 200, "日程关键词")
            for word in items(rule["keywords"], "日程关键词")
        ]
        if any(not word for word in keywords):
            raise ValueError("日程关键词不能为空")
        rules.append({"session": origin, "mode": rule["mode"], "keywords": keywords})
    extensions["calendar_reminders"] = rules
    club = object_fields(
        calendar["club"],
        {"daily", "weekly", "daily_hour", "weekly_hour", "weekly_day"},
        "社团提醒",
    )
    extensions["club"] = {
        "daily": flag(club["daily"], "次日提醒"),
        "weekly": flag(club["weekly"], "一周预告"),
        "daily_hour": integer(club["daily_hour"], 0, 23, "次日提醒小时"),
        "weekly_hour": integer(club["weekly_hour"], 0, 23, "一周预告小时"),
        "weekly_day": integer(club["weekly_day"], 0, 6, "一周预告星期"),
    }

    repair = object_fields(
        document["repair"],
        {
            "queries_enabled",
            "since",
            "reminder_enabled",
            "hour",
            "reminder_sessions",
            "event_sessions",
            "raw_session",
            "threshold_days",
        },
        "维修",
    )
    candidate["tickets_enabled"] = flag(repair["queries_enabled"], "工单查询")
    since = text(repair["since"], 64, "工单起始日期")
    if since:
        try:
            parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("工单起始日期应为带时区的 ISO 时间") from None
        if parsed.tzinfo is None:
            raise ValueError("工单起始日期必须包含时区，例如 +08:00")
    reminder_enabled = flag(repair["reminder_enabled"], "维修超期提醒")
    if (candidate["tickets_enabled"] or reminder_enabled) and not since:
        raise ValueError("启用工单查询或超期提醒需要填写起始日期")
    thresholds = object_fields(
        repair["threshold_days"], {"open", "accepted", "committed"}, "工单超期天数"
    )
    for value in thresholds.values():
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ValueError("超期天数应为非负有限数值")
    raw_session = text(repair["raw_session"], 512, "维修原始事件目标")
    if raw_session:
        sessions([raw_session], allowed, "维修原始事件目标")
    extensions["repair"].update(
        since=since,
        reminder_enabled=reminder_enabled,
        hour=integer(repair["hour"], 0, 23, "维修提醒小时"),
        raw_session=raw_session,
        reminder_sessions=sessions(
            repair["reminder_sessions"], allowed, "维修提醒目标"
        ),
        event_sessions=sessions(repair["event_sessions"], allowed, "维修事件目标"),
        threshold_days=copy.deepcopy(thresholds),
    )

    bridge = object_fields(document["bridge"], {"clients"}, "游戏桥")
    clients = items(bridge["clients"], "游戏桥服务器")
    if len(clients) != len(current["minecraft_clients"]):
        raise ValueError("服务器列表已变化，请重新读取页面")
    indexes = set()
    for client in clients:
        object_fields(client, {"index", "groups"}, "游戏桥服务器")
        index = integer(client["index"], 0, len(clients) - 1, "游戏桥服务器索引")
        if index in indexes:
            raise ValueError("游戏桥服务器重复")
        indexes.add(index)
        original = current["minecraft_clients"][index]
        old_rules = {rule["session"]: rule for rule in original.get("groups", [])}
        updated = []
        for rule in items(client["groups"], "游戏桥目标"):
            object_fields(
                rule,
                {
                    "session",
                    "legacy_group_id",
                    "enabled",
                    "to_client",
                    "from_client",
                    "disabled_events",
                },
                "游戏桥目标",
            )
            origin = sessions([rule["session"]], allowed, "游戏桥目标")[0]
            if origin in {row["session"] for row in updated}:
                raise ValueError("游戏桥目标重复")
            disabled = items(rule["disabled_events"], "游戏事件")
            if any(
                not isinstance(value, str) or value not in EVENTS for value in disabled
            ) or len(set(disabled)) != len(disabled):
                raise ValueError("游戏事件类型不正确或重复")
            old = old_rules.get(origin, {})
            effective = bridge_group(old, runtime)["disabled_events"] if old else []
            value = {
                **copy.deepcopy(old),
                "session": origin,
                "legacy_group_id": text(rule["legacy_group_id"], 128, "旧游戏桥群号"),
                "enabled": flag(rule["enabled"], "游戏桥目标开关"),
                "to_client": flag(rule["to_client"], "转发到游戏"),
                "from_client": flag(rule["from_client"], "转发到群"),
                "disabled_events": sorted(disabled),
            }
            if sorted(disabled) != effective:
                # Persist the panel edit without deleting legacy command overrides.
                value["events_revision"] = uuid.uuid4().hex
            updated.append(value)
        extensions["minecraft_clients"][index]["groups"] = updated

    summary = object_fields(
        document["summary"],
        {"enabled", "scheduled", "sessions", "hour", "minute", "min_messages"},
        "每日总结",
    )
    extensions["summary"].update(
        enabled=flag(summary["enabled"], "聊天记录"),
        scheduled=flag(summary["scheduled"], "定时总结"),
        sessions=sessions(summary["sessions"], allowed, "总结目标"),
        hour=integer(summary["hour"], 0, 23, "总结小时"),
        minute=integer(summary["minute"], 0, 59, "总结分钟"),
        min_messages=integer(summary["min_messages"], 0, 2147483647, "总结最少消息数"),
    )
    candidate["extensions_json"] = json.dumps(extensions, ensure_ascii=False, indent=2)
    return candidate, normalize(candidate)


class PushRulesPanel:
    def __init__(self, plugin, raw_config):
        self.plugin, self.raw_config = plugin, raw_config
        self.lock = asyncio.Lock()

    def snapshot(self):
        config, runtime = self.plugin.config, self.plugin.runtime
        rules = read_rules(config, runtime)
        revision = hashlib.sha256(
            json.dumps(
                {"raw": dict(self.raw_config), "rules": rules},
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        github = config["github"]
        client = getattr(runtime, "github_client", None) if runtime else None
        socket = getattr(client, "socket", None)
        return {
            "rules": rules,
            "revision": revision,
            "allowed_sessions": config.get("allowed_sessions", []),
            "status": {
                "running": runtime is not None,
                "queue": runtime.store.counts() if runtime else {},
                "github_enabled": github["enabled"],
                "github_configured": bool(github.get("address")),
                "github_connected": socket is not None and not socket.closed,
                "nsq_configured": bool(config["repair"]["nsq"].get("enabled")),
                "bridge": [
                    {
                        "index": index,
                        "connected": any(
                            c.config is cfg
                            and c.socket is not None
                            and not c.socket.closed
                            for c in runtime.bridges
                        )
                        if runtime
                        else False,
                    }
                    for index, cfg in enumerate(config["minecraft_clients"])
                ],
            },
        }

    async def save(self, payload):
        object_fields(payload, {"revision", "rules"}, "保存请求")
        async with self.lock:
            if payload["revision"] != self.snapshot()["revision"]:
                raise RulesConflict("配置已被其他页面或命令修改，请重新读取后再保存")
            candidate, normalized = build_candidate(
                self.raw_config, payload["rules"], self.plugin.runtime
            )
            previous = copy.deepcopy(dict(self.raw_config))
            try:
                self.raw_config.clear()
                self.raw_config.update(candidate)
                self.raw_config.save_config()
            except Exception:
                self.raw_config.clear()
                self.raw_config.update(previous)
                raise
            # Preserve dictionaries captured by running game-client callbacks.
            for index, old in enumerate(self.plugin.config["minecraft_clients"]):
                old["groups"] = normalized["minecraft_clients"][index]["groups"]
                normalized["minecraft_clients"][index] = old
            self.plugin.config.clear()
            self.plugin.config.update(normalized)
            runtime = self.plugin.runtime
            if runtime:
                runtime.summary.config = normalized["summary"]
                runtime.github_presentation.config = normalized["github"]
            return self.snapshot()
