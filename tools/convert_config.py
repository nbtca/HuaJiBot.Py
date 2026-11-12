"""Convert an explicit .NET plugin configuration export; never read live secrets implicitly."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from configuration import DEFAULT_EXTENSIONS, normalize  # noqa: E402


def convert(source, mapping):
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    result = {k: v["default"] for k, v in schema.items()}
    extra = json.loads(json.dumps(DEFAULT_EXTENSIONS))

    def session(value):
        if not value:
            raise ValueError("Empty legacy group ID")
        if value in mapping:
            value = mapping[value]
        if not isinstance(value, str) or ":GroupMessage:" not in value:
            raise ValueError(
                "Missing session mapping for a legacy group; populate --session-map"
            )
        if value not in result["allowed_sessions"]:
            result["allowed_sessions"].append(value)
        return value

    cal = source.get("Calendar", {})
    result["calendar_enabled"] = cal.get("Enabled", True)
    extra["calendar_reminders"] = [
        {
            "session": session(g["GroupId"]),
            "mode": {
                "WhiteList": "whitelist",
                "BlackList": "blacklist",
                "Default": "all",
            }.get(g.get("Mode", "WhiteList"), "whitelist"),
            "keywords": g.get("Keywords", []),
        }
        for g in cal.get("ReminderGroups", [])
    ]
    club = cal.get("ClubAffairsReminder", {})
    for old, new in [
        ("EnableDailyReminder", "daily"),
        ("EnableWeeklySummary", "weekly"),
        ("DailyReminderHour", "daily_hour"),
        ("WeeklySummaryHour", "weekly_hour"),
        ("WeeklySummaryDayOfWeek", "weekly_day"),
    ]:
        if old in club:
            value = club[old]
            if new == "weekly_day" and isinstance(value, str):
                value = [
                    "Sunday",
                    "Monday",
                    "Tuesday",
                    "Wednesday",
                    "Thursday",
                    "Friday",
                    "Saturday",
                ].index(value)
            extra["club"][new] = value
    repair = source.get("RepairTeam", {})
    result["tickets_enabled"] = bool(
        repair.get("Enabled", True) and repair.get("RemindSince")
    )
    result["tickets_url"] = repair.get("SaturdayApi", result["tickets_url"])
    r = extra["repair"]
    r.update(
        since=repair.get("RemindSince") or "",
        hour=repair.get("RemindHour", 12),
        reminder_enabled=result["tickets_enabled"],
        reminder_sessions=[session(g) for g in repair.get("RemindGroups", [])],
        event_sessions=[session(g) for g in repair.get("PushInfoGroup", [])],
        threshold_days={
            "open": repair.get("OpenDays", 1),
            "accepted": repair.get("AcceptedDays", 3),
            "committed": repair.get("CommittedDays", 2),
        },
    )
    r["nsq"].update(
        enabled=bool(
            repair.get("Enabled", True)
            and repair.get("NsqUrl")
            and repair.get("NsqTopic")
        ),
        address=repair.get("NsqUrl", ""),
        topic=repair.get("NsqTopic", ""),
        channel=repair.get("NsqChannel", ""),
        secret=repair.get("NsqSecret", ""),
    )
    r["raw_session"] = (
        session(repair["PushRawGroup"]) if repair.get("PushRawGroup") else ""
    )
    github = source.get("GitHubBridge", {})
    extra["github"].update(
        enabled=bool(github.get("Enabled", True) and github.get("Address")),
        address=github.get("Address", ""),
        token=github.get("AuthBearer", ""),
        routes=github.get("BroadcastMap", {}),
        groups={
            name: [session(g) for g in groups]
            for name, groups in github.get("BroadcastGroup", {}).items()
        },
        short_link={
            "enabled": bool(
                github.get("ShortLinkToken") or github.get("ShortLinkAccessClientId")
            ),
            "api": github.get("ShortLinkApi", "https://link.nbtca.space/api/shorten"),
            "token": github.get("ShortLinkToken", ""),
            "access_client_id": github.get("ShortLinkAccessClientId", ""),
            "access_client_secret": github.get("ShortLinkAccessClientSecret", ""),
        },
    )
    bridge = source.get("MessageBridge", {})
    for client in bridge.get("Clients", []):
        extra["minecraft_clients"].append(
            {
                "enabled": bridge.get("Enabled", True),
                "address": client["Address"],
                "token": client.get("Token", ""),
                "groups": [
                    {
                        "session": session(g["GroupId"]),
                        "legacy_group_id": g["GroupId"],
                        "enabled": g.get("Enabled", True),
                        "to_client": g.get("ForwardToClient", True),
                        "from_client": g.get("ForwardFromClient", True),
                        "disabled_events": g.get("ForwardFromClientDisabledEvent", []),
                    }
                    for g in client.get("Groups", [])
                ],
            }
        )
    summary = source.get("每日聊天总结", source.get("DailySummary", {}))
    s = extra["summary"]
    s.update(
        enabled=bool(summary.get("Enabled", True) and summary.get("GroupIds")),
        scheduled=bool(summary.get("GroupIds")),
        sessions=[session(g) for g in summary.get("GroupIds", [])],
    )
    for old, new in [
        ("SummaryHour", "hour"),
        ("SummaryMinute", "minute"),
        ("MinMessageCount", "min_messages"),
        ("MaxRetryCount", "max_retries"),
        ("LlmTimeoutSeconds", "timeout_seconds"),
        ("MaxPromptChars", "max_prompt_chars"),
        ("SystemPrompt", "system_prompt"),
    ]:
        if old in summary:
            s[new] = summary[old]
    result["extensions_json"] = json.dumps(extra, ensure_ascii=False, indent=2)
    normalize(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source", type=Path, help="JSON object keyed by .NET plugin names"
    )
    parser.add_argument(
        "--session-map",
        type=Path,
        required=True,
        help="JSON object: old group ID -> AstrBot UMO",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Must end with .local.json; may contain service secrets",
    )
    args = parser.parse_args()
    if not args.output.name.endswith(".local.json"):
        parser.error("Output must end with .local.json (gitignored)")
    if args.output.exists():
        parser.error(
            "Output exists; use a new filename to preserve existing configuration"
        )
    result = convert(
        json.loads(args.source.read_text(encoding="utf-8-sig")),
        json.loads(args.session_map.read_text(encoding="utf-8-sig")),
    )
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "Configuration converted. Add official admin UIDs separately; credentials were not printed."
    )


if __name__ == "__main__":
    main()
