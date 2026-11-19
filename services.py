"""Business services independent of AstrBot; all outbound requests are read-only."""

import asyncio
import json
import time
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from dateutil.rrule import rrulestr
from icalendar import Calendar
from mcstatus import JavaServer

TZ = ZoneInfo("Asia/Shanghai")


def bounded_text(value, limit=180):
    return str(value or "").replace("\r", " ").replace("\n", " ")[:limit]


def player_line(player):
    ping = player.get("ping")
    latency = f"{ping} ms" if type(ping) is int and ping >= 0 else "未知"
    name = bounded_text(player.get("name"), 64) or "未知玩家"
    world = bounded_text(player.get("world")) or "未知"
    return f"{name} · {latency} · {world}"


def player_list(name, players):
    return "\n".join(
        [f"{name}：{len(players)} 人在线", *(player_line(p) for p in players)]
    )


def snapshot_players(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("snapshot too large")
    report = json.loads(raw)
    if not isinstance(report, dict) or type(report.get("timestamp")) is not int:
        raise ValueError("invalid snapshot")
    if not 0 <= time.time() - report["timestamp"] <= 30:
        raise ValueError("expired snapshot")
    players = report.get("players")
    if not isinstance(players, list) or not all(isinstance(p, dict) for p in players):
        raise ValueError("invalid players")
    return players


def local_datetime(value):
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
        )
    if isinstance(value, date):
        return datetime.combine(value, dt_time.min, TZ)
    raise ValueError("Invalid calendar date")


def calendar_text(raw, now, days=7, only_next=False):
    cal = Calendar.from_ical(raw)
    start_window, end_window = sorted((now, now + timedelta(days=days)))
    events = expand_calendar(cal, start_window, end_window)
    rows = []
    for event in events:
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start = local_datetime(event.decoded("DTSTART"))
        end = local_datetime(event.decoded("DTEND", event.decoded("DTSTART")))
        if days >= 0 and end <= now and start < now:
            continue
        all_day = not isinstance(event.decoded("DTSTART"), datetime)
        when = start.strftime("%m-%d 全天" if all_day else "%m-%d %H:%M")
        text = f"{when} · {bounded_text(event.get('SUMMARY', '未命名日程'))}"
        if event.get("LOCATION"):
            text += f"\n地点：{bounded_text(event.get('LOCATION'))}"
        if only_next and event.get("DESCRIPTION"):
            text += f"\n说明：{event.get('DESCRIPTION')}"
        rows.append((start, text))
    rows.sort(key=lambda row: row[0])
    if not rows:
        return f"{'未来' if days >= 0 else '过去'} {abs(days)} 天没有日程。"
    selected = rows[:1] if only_next else rows
    result = (
        "最近日程"
        if only_next
        else f"{'未来' if days >= 0 else '过去'} {abs(days)} 天日程"
    )
    result += "\n" + "\n\n".join(row[1] for row in selected)
    return result


def expand_calendar(cal, start_window, end_window):
    """Expand bounded RFC5545 recurrence, including EXDATE/RDATE and overrides."""
    source = list(cal.walk("VEVENT"))
    overrides = {}
    for event in source:
        if event.get("RECURRENCE-ID"):
            overrides[
                (str(event.get("UID")), local_datetime(event.decoded("RECURRENCE-ID")))
            ] = event
    result = []
    for event in source:
        if event.get("RECURRENCE-ID") or not event.get("DTSTART"):
            continue
        start = local_datetime(event.decoded("DTSTART"))
        end = local_datetime(event.decoded("DTEND", event.decoded("DTSTART")))
        duration = event.decoded("DURATION", end - start)
        if not isinstance(event.decoded("DTSTART"), datetime) and not duration:
            duration = timedelta(days=1)
        dates = {start}
        if event.get("RRULE"):
            seed = event.decoded("DTSTART")
            if isinstance(seed, datetime):
                seed = seed if seed.tzinfo else seed.replace(tzinfo=TZ)
            else:
                seed = datetime.combine(seed, dt_time.min)
            rule = rrulestr(event["RRULE"].to_ical().decode(), dtstart=seed)
            for index, occurrence in enumerate(rule):
                occurrence = local_datetime(occurrence)
                if index >= 20000:
                    raise ValueError("重复日程过于密集，无法在限制内展开")
                if occurrence > end_window:
                    break
                if occurrence + duration >= start_window:
                    dates.add(occurrence)
        for field, remove in (("RDATE", False), ("EXDATE", True)):
            props = event.get(field, [])
            if not isinstance(props, list):
                props = [props]
            for prop in props:
                for item in prop.dts:
                    value = local_datetime(item.dt)
                    if remove:
                        dates.discard(value)
                    else:
                        dates.add(value)
        for occurrence in dates:
            if (str(event.get("UID")), occurrence) in overrides:
                continue
            if occurrence > end_window or occurrence + duration < start_window:
                continue
            instance = event.copy()
            instance.pop("DTSTART", None)
            instance.pop("DTEND", None)
            all_day = not isinstance(event.decoded("DTSTART"), datetime)
            instance.add("DTSTART", occurrence.date() if all_day else occurrence)
            instance.add(
                "DTEND",
                (occurrence + duration).date() if all_day else occurrence + duration,
            )
            result.append(instance)
    for event in overrides.values():
        if (
            event.get("DTSTART")
            and start_window <= local_datetime(event.decoded("DTSTART")) <= end_window
        ):
            result.append(event)
    return result


class Services:
    def __init__(self, config):
        self.config = config
        self.http = None
        self.cache = {}
        self.lock = asyncio.Lock()

    async def start(self):
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self):
        if self.http:
            await self.http.close()

    async def fetch(self, url, ttl=60):
        if not url.startswith(("https://", "http://")):
            raise ValueError("请配置 HTTP(S) 服务地址")
        async with self.lock:
            cached = self.cache.get(url)
            if cached and time.monotonic() - cached[0] < ttl:
                return cached[1]
            if self.http is None:
                raise RuntimeError("服务未初始化")
            async with self.http.get(url) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    content.extend(chunk)
                    if len(content) > 2_000_000:
                        raise ValueError("服务响应超过 2 MB")
            if len(self.cache) >= 100:
                self.cache.clear()
            raw = bytes(content)
            self.cache[url] = (time.monotonic(), raw)
            return raw

    async def calendar(self, days=7, only_next=False):
        if not self.config.get("calendar_enabled", False):
            return "日程功能未启用。"
        raw = await self.fetch(self.config.get("calendar_url", ""), ttl=300)
        return await asyncio.to_thread(
            calendar_text, raw, datetime.now(TZ), days, only_next
        )

    async def minecraft(self, bridge_players=None):
        address = self.config.get("minecraft_address", "").strip()
        targets = self.config.get("minecraft_servers", [])
        if not self.config.get("minecraft_enabled", False) or not (targets or address):
            return "MC 查询未配置。"
        if targets:
            bridge_players = bridge_players or {}
            results = await asyncio.gather(
                *(
                    self.minecraft_status(target, bridge_players.get(target["name"]))
                    for target in targets
                ),
                return_exceptions=True,
            )
            return "\n".join(
                f"{target['name']}\n状态查询失败（{type(result).__name__}）"
                if isinstance(result, Exception)
                else result
                for target, result in zip(targets, results, strict=True)
            )
        return await self.minecraft_status({"name": "Minecraft", "address": address})

    async def minecraft_status(self, target, bridge_players=None):
        address = target["address"].strip()
        name = target["name"]
        if bridge_players is not None:
            return player_list(name, bridge_players)
        status = None
        try:
            server = await JavaServer.async_lookup(address, timeout=5)
            status = await server.async_status()
            lines = [f"{name}：{status.players.online} 人在线"]
        except Exception as exc:
            lines = [f"{name}：状态查询失败（{type(exc).__name__}）"]
        if path := target.get("player_snapshot"):
            try:
                players = await asyncio.to_thread(snapshot_players, path)
            except (OSError, ValueError):
                lines.append("玩家详情暂不可用（快照缺失、过期或无效）")
            else:
                return player_list(name, players)
        if status and (sample := getattr(status.players, "sample", None)):
            lines.append("状态响应中的玩家样本：")
            lines.extend(bounded_text(p.name, 64) for p in sample)
        return "\n".join(lines)
