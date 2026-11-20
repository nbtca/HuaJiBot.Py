"""Ports of Calendar filtering, RepairTeam/Saturday and event text formatting."""

from datetime import datetime, timedelta

from .services import bounded_text, local_datetime, notification_allowed

STAGES = {"open": "待接单", "accepted": "已接单未提交", "committed": "待审核"}
ACTIONS = {
    "create": "新报修",
    "accept": "已接单",
    "cancel": "报修人已取消",
    "drop": "队员已放弃，重新待接单",
    "commit": "维修完成，待审核",
    "alterCommit": "维修记录已修改，待审核",
    "reject": "审核退回，需重新处理",
    "close": "审核通过，已结单",
    "update": "报修信息已更新",
}


def timestamp(value):
    if not value:
        raise ValueError("Missing service timestamp")
    return local_datetime(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


async def active_tickets(fetch_json, base, since, max_pages=200):
    """Read all active pages and detail records; failures do not pretend completeness."""
    result = {}
    for status in STAGES:
        for page_index in range(max_pages):
            page = await fetch_json(
                f"{base.rstrip('/')}/events?status={status}&order=DESC&limit=50&offset={page_index * 50}"
            )
            if not isinstance(page, list):
                raise ValueError("Invalid Saturday list")
            for item in page:
                if timestamp(item["gmtCreate"]) < since:
                    continue
                detail = await fetch_json(
                    f"{base.rstrip('/')}/events/{int(item['eventId'])}"
                )
                if detail.get("status") not in STAGES:
                    continue  # Ticket may have closed between list and detail requests.
                last = max(
                    [timestamp(detail["gmtCreate"])]
                    + [timestamp(log["gmtCreate"]) for log in detail.get("logs", [])]
                )
                result[detail["eventId"]] = {**detail, "last_activity": last}
            if len(page) < 50:
                break
        else:
            raise ValueError("工单分页超过上限，拒绝返回不完整汇总")
    return list(result.values())


def stalled(tickets, now, thresholds):
    return [
        t
        for t in tickets
        if now - t["last_activity"]
        > timedelta(
            days=float(
                thresholds.get(
                    t["status"], {"open": 1, "accepted": 3, "committed": 2}[t["status"]]
                )
            )
        )
    ]


def ticket_digest(tickets, now, title="维修工单", portal="https://repair.nbtca.space"):
    if not tickets:
        return "没有进行中的工单"
    lines = [f"【{title}】{len(tickets)} 张"]
    for status, label in STAGES.items():
        items = sorted(
            (t for t in tickets if t["status"] == status),
            key=lambda t: t["last_activity"],
        )
        if not items:
            continue
        lines.append(label + "：")
        for t in items:
            member = (t.get("member") or {}).get("alias", "")
            age = max(0, (now - t["last_activity"]).days)
            fields = [
                t.get("model"),
                t.get("problem"),
                member,
                f"已 {age} 天" if age else "不到 1 天",
            ]
            lines.append(
                f"#{t['eventId']} " + " · ".join(bounded_text(x) for x in fields if x)
            )
    return "\n".join(lines + [f"去处理：{portal}"])


def repair_event(data):
    lines = [
        f"【维修 #{data.get('event_id', '?')}】{ACTIONS.get(data.get('action'), data.get('action', '未知动作'))}"
    ]
    for label, key in [
        ("操作人", "member_alias"),
        ("机型", "model"),
        ("问题", "problem"),
        ("说明", "description"),
    ]:
        if data.get(key):
            lines.append(f"{label}：{bounded_text(data[key], 800)}")
    return "\n".join(lines)


def calendar_matches(event, rule):
    text = "\n".join(
        str(event.get(key, "")) for key in ["SUMMARY", "DESCRIPTION", "LOCATION"]
    )
    hit = any(keyword in text for keyword in rule.get("keywords", []) if keyword)
    mode = rule.get("mode", "all")
    return hit if mode == "whitelist" else not hit if mode == "blacklist" else True


def event_text(event):
    start = local_datetime(event.decoded("DTSTART"))
    label = (
        start.strftime("%m-%d %H:%M")
        if isinstance(event.decoded("DTSTART"), datetime)
        else start.strftime("%m-%d 全天")
    )
    return f"{label} {event.get('SUMMARY', '未命名日程')}\n{event.get('LOCATION', '')}\n{event.get('DESCRIPTION', '')}".strip()


def calendar_notifications(events, rules, now, club):
    """Yield stable per-occurrence keys and scheduled due times, never send directly."""
    for rule in rules:
        origin = rule["session"]
        matched = [
            e for e in events if notification_allowed(e) and calendar_matches(e, rule)
        ]
        for event in matched:
            start = local_datetime(event.decoded("DTSTART"))
            if not isinstance(event.decoded("DTSTART"), datetime):
                continue
            points = [("start", start - timedelta(minutes=60), "将在 60 分钟后开始")]
            if event.get("DTEND"):
                points.append(
                    (
                        "end",
                        local_datetime(event.decoded("DTEND")) - timedelta(minutes=5),
                        "预计 5 分钟后结束",
                    )
                )
            for kind, due, label in points:
                if now - timedelta(minutes=2) <= due <= now + timedelta(minutes=15):
                    key = f"calendar:{event.get('UID')}:{start.isoformat()}:{kind}:{origin}"
                    yield (
                        key,
                        origin,
                        f"日程提醒：{label}\n{event_text(event)}",
                        due.timestamp(),
                    )
        tomorrow = now.date() + timedelta(days=1)
        if club.get("daily", False) and now.hour == club.get("daily_hour", 9):
            rows = [
                e
                for e in matched
                if local_datetime(e.decoded("DTSTART")).date() == tomorrow
            ]
            if rows:
                yield (
                    f"club:daily:{now.date()}:{origin}",
                    origin,
                    "【明天截止】\n" + "\n\n".join(map(event_text, rows)),
                    now.timestamp(),
                )
        # .NET Sunday=0; Python Monday=0.
        if (
            club.get("weekly", False)
            and (now.weekday() + 1) % 7 == club.get("weekly_day", 1)
            and now.hour == club.get("weekly_hour", 9)
        ):
            rows = [
                e
                for e in matched
                if now <= local_datetime(e.decoded("DTSTART")) < now + timedelta(days=7)
            ]
            if rows:
                yield (
                    f"club:weekly:{now.date()}:{origin}",
                    origin,
                    "【社团事务一周预告】\n" + "\n\n".join(map(event_text, rows)),
                    now.timestamp(),
                )


def github_message(kind, body):
    from .github_bridge import build_post, should_broadcast

    return build_post(kind, body).text() if should_broadcast(kind, body) else None


def github_targets(body, config):
    repo = body.get("repository", {})
    routes = config.get("routes", {})
    name = repo.get("full_name", "")
    if name not in routes and repo.get("private", False):
        return []
    return config.get("groups", {}).get(routes.get(name, "default"), [])
