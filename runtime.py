"""Own lifecycle, scheduled jobs and durable delivery for the migrated plugins."""

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta

from aiohttp import web
from icalendar import Calendar

from .business import (
    active_tickets,
    calendar_notifications,
    github_message,
    github_targets,
    repair_event,
    stalled,
    ticket_digest,
    timestamp,
)
from .routing import parse_command
from .services import TZ, expand_calendar
from .state import State
from .summaries import Summaries
from .transports import EVENT_TYPES, MQClient, NSQConsumer, minecraft_message, packet


class Runtime:
    def __init__(self, config, services, data_path, send, llm, log):
        self.config, self.services, self.send, self.log = config, services, send, log
        data_path.mkdir(parents=True, exist_ok=True)
        self.store = State(data_path / "huaji.db")
        self.summary = Summaries(self.store, config.get("summary", {}), llm)
        self.tasks, self.bridges = [], []
        self.runner = None
        self.observed = {}
        self.last_prune = 0
        self.scheduler_busy = asyncio.Lock()

    def allowed(self, origin):
        return origin in self.config.get("allowed_sessions", [])

    async def start(self):
        try:
            for cfg in self.config.get("minecraft_clients", []):
                if not cfg.get("enabled", True):
                    continue

                async def receive(value, cfg=cfg):
                    await self.minecraft_event(cfg, value)

                client = MQClient(self.services.http, cfg, receive, self.log)
                self.bridges.append(client)
                self.tasks.append(asyncio.create_task(client.run()))
            github = self.config.get("github", {})
            if github.get("enabled") and github.get("address"):

                async def receive_github(value):
                    if value.get("type") == "webhook":
                        wrapped = value["data"]
                        headers = {
                            k.lower(): v for k, v in wrapped.get("headers", {}).items()
                        }

                        def header(name):
                            value = headers.get(name, "")
                            return (
                                value[0] if isinstance(value, list) and value else value
                            )

                        await self.github_event(
                            header("x-github-event"),
                            wrapped["body"],
                            header("x-github-delivery"),
                        )

                client = MQClient(self.services.http, github, receive_github, self.log)
                self.tasks.append(asyncio.create_task(client.run()))
            nsq = self.config.get("repair", {}).get("nsq", {})
            if nsq.get("enabled"):
                self.tasks.append(
                    asyncio.create_task(
                        NSQConsumer(nsq, self.repair_event, self.log).run()
                    )
                )
            await self.start_http()
            self.tasks += [
                asyncio.create_task(self.delivery_loop()),
                asyncio.create_task(self.scheduler_loop()),
            ]
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        self.store.close()

    def enqueue(self, key, origin, text, due=None):
        if not self.allowed(origin):
            return False
        if not isinstance(text, str) or not text.strip() or len(text) > 60000:
            raise ValueError("Invalid notification text size")
        self.store.bind(origin)
        # Freeze accepted content. Future polls must not mutate a pending/sent delivery.
        if self.store.get("accepted:" + key):
            return True
        self.store.enqueue_parts(key, origin, text, due or time.time())
        return True

    async def delivery_once(self):
        item = self.store.next()
        if item is None:
            return False
        key, origin, text, attempts = item
        if not self.allowed(origin):
            self.store.finish(key, "cancelled")
            return True
        try:
            async with asyncio.timeout(45):
                if await self.send(origin, text) is False:
                    raise ConnectionError("No platform for session")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.retry(key, attempts + 1)
            self.log(f"Notification failed ({attempts + 1}/3): {type(exc).__name__}")
        else:
            self.store.finish(key, "sent")
        return True

    async def delivery_loop(self):
        while True:
            try:
                await self.delivery_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"Delivery worker error: {type(exc).__name__}")
            await asyncio.sleep(2)

    async def github_event(self, kind, body, delivery=""):
        cfg = self.config.get("github", {})
        if not cfg.get("enabled"):
            return
        text = github_message(kind, body)
        if text is None:
            return
        if not delivery:
            delivery = hashlib.sha256(
                json.dumps([kind, body], sort_keys=True).encode()
            ).hexdigest()
        for origin in github_targets(body, cfg):
            self.enqueue(f"github:{delivery}:{origin}", origin, text)

    async def repair_event(self, delivery, body):
        for origin in self.config.get("repair", {}).get("event_sessions", []):
            self.enqueue(f"repair:{delivery}:{origin}", origin, repair_event(body))

    async def minecraft_event(self, cfg, value):
        text = minecraft_message(value)
        if text is None:
            return
        # Source protocol has no event ID. Do not collapse identical legitimate chats.
        import uuid

        delivery = uuid.uuid4().hex
        for rule in cfg.get("groups", []):
            disabled = self.store.get(
                "mc-events:" + rule["session"], rule.get("disabled_events", [])
            )
            if (
                rule.get("enabled", True)
                and rule.get("from_client", True)
                and EVENT_TYPES.get(value["type"]) not in disabled
            ):
                self.enqueue(
                    f"minecraft:{delivery}:{rule['session']}", rule["session"], text
                )

    async def observe(
        self, origin, sender, name, text, message_id, observed_at, is_bot=False
    ):
        if not self.allowed(origin) or is_bot:
            return
        now = time.monotonic()
        self.observed = {key: t for key, t in self.observed.items() if now - t < 300}
        key = (origin, message_id)
        if message_id and key in self.observed:
            return
        if message_id:
            self.observed[key] = now
        summary = self.config.get("summary", {})
        if summary.get("enabled") and origin in summary.get("sessions", []):
            self.store.record(
                origin,
                message_id
                or hashlib.sha256(
                    f"{observed_at}:{sender}:{text}".encode()
                ).hexdigest(),
                observed_at,
                name,
                text,
            )
        if self.config.get("bridge_skip_commands", True) and parse_command(text):
            return
        for client in self.bridges:
            rules = [
                r
                for r in client.config.get("groups", [])
                if r.get("session") == origin
                and r.get("enabled", True)
                and r.get("to_client", True)
            ]
            if not rules:
                continue
            # Mapping must be explicitly supplied if the game bridge expects a numeric group ID.
            rule = rules[0]
            value = packet(
                "group_message",
                {
                    "group_id": rule.get("legacy_group_id") or origin,
                    "group_name": rule.get("name", "QQ群"),
                    "sender_id": sender,
                    "sender_name": name,
                    "message": text,
                },
            )
            try:
                await client.send(value)
            except Exception as exc:
                self.log(f"Game bridge send failed: {type(exc).__name__}")

    async def players(self, origin):
        clients = [
            c
            for c in self.bridges
            if any(
                r.get("session") == origin and r.get("enabled", True)
                for r in c.config.get("groups", [])
            )
        ]
        if not clients:
            return None
        results = await asyncio.gather(
            *(c.query() for c in clients), return_exceptions=True
        )
        lines = []
        for batch in results:
            if isinstance(batch, Exception):
                lines.append("有一个服务器连接不可用。")
                continue
            for value in batch:
                data = value.get("data", {})
                name = (value.get("source") or {}).get("display_name", "服务器")
                players = data.get("players", [])
                lines.append(
                    f"{name} 玩家列表：" if players else f"{name} 没有玩家在线！"
                )
                lines += [
                    f"{p.get('name', '?')} {p.get('ping', '?')}ms {p.get('world', '?')}"
                    for p in players
                ]
        return "\n".join(lines) or "查询超时！"

    async def tickets(self):
        cfg = self.config.get("repair", {})
        if not cfg.get("since"):
            raise ValueError("未配置维修查询起始日期 repair.since")

        async def fetch_json(url):
            return json.loads(await self.services.fetch(url, ttl=60))

        return await active_tickets(
            fetch_json,
            self.config.get("tickets_url", "https://api.nbtca.space"),
            timestamp(cfg["since"]),
            cfg.get("max_pages", 200),
        )

    async def scheduler_loop(self):
        while True:
            await self.tick(datetime.now(TZ))
            await asyncio.sleep(60)

    async def tick(self, now):
        async with self.scheduler_busy:
            for job in (self.calendar_tick, self.repair_tick, self.summary_tick):
                try:
                    await job(now)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.log(f"{job.__name__} failed: {type(exc).__name__}")
            if now.timestamp() - self.last_prune >= 3600:
                retention = max(
                    1, int(self.config.get("summary", {}).get("retention_days", 7))
                )
                self.store.prune((now - timedelta(days=retention)).timestamp())
                self.last_prune = now.timestamp()

    async def calendar_tick(self, now):
        rules = self.config.get("calendar_reminders", [])
        if not self.config.get("calendar_enabled") or not rules:
            return
        raw = await self.services.fetch(self.config["calendar_url"], ttl=300)
        events = await asyncio.to_thread(
            lambda: expand_calendar(
                Calendar.from_ical(raw),
                now - timedelta(days=1),
                now + timedelta(days=8),
            )
        )
        for key, origin, text, due in calendar_notifications(
            events, rules, now, self.config.get("club", {})
        ):
            self.enqueue(key, origin, text, due)

    async def repair_tick(self, now):
        cfg = self.config.get("repair", {})
        if not cfg.get("reminder_enabled") or now.hour != cfg.get("hour", 12):
            return
        targets = [
            o
            for o in cfg.get("reminder_sessions", [])
            if self.allowed(o) and not self.store.get(f"repair-day:{now.date()}:{o}")
        ]
        if not targets:
            return
        tickets = stalled(await self.tickets(), now, cfg.get("threshold_days", {}))
        for origin in targets:
            if tickets:
                self.enqueue(
                    f"repair-day:{now.date()}:{origin}",
                    origin,
                    ticket_digest(tickets, now, "维修工单提醒"),
                )
            self.store.set(f"repair-day:{now.date()}:{origin}", True)

    async def summary_tick(self, now):
        cfg = self.config.get("summary", {})
        if (
            not cfg.get("enabled")
            or not cfg.get("scheduled")
            or now.hour != cfg.get("hour", 0)
            or now.minute < cfg.get("minute", 0)
        ):
            return
        day = now.date() - timedelta(days=1)
        for origin in cfg.get("sessions", []):
            if not self.allowed(origin):
                continue
            key = f"summary:{day}:{origin}"
            if self.store.get(key + ":done"):
                continue
            attempts = self.store.get(key + ":attempts", 0)
            if attempts >= cfg.get("max_retries", 3):
                continue
            self.store.set(key + ":attempts", attempts + 1)
            try:
                text = await self.summary.generate(origin, day)
                if text:
                    self.enqueue(key, origin, text)
                self.store.set(key + ":done", True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"Summary attempt failed: {type(exc).__name__}")

    async def start_http(self):
        cfg = self.config.get("push_api", {})
        if not cfg.get("enabled"):
            return
        if len(cfg.get("token", "")) < 24:
            raise ValueError("推送接口 token 至少 24 字符")

        async def push(request):
            if not hmac.compare_digest(
                request.headers.get("Authorization", ""), "Bearer " + cfg["token"]
            ):
                raise web.HTTPUnauthorized()
            try:
                data = await request.json()
                key, origin, text = data["id"], data["session"], data["text"]
                if (
                    not all(isinstance(x, str) for x in (key, origin, text))
                    or not 1 <= len(key) <= 128
                ):
                    raise ValueError("Invalid payload")
                if not self.allowed(origin):
                    raise web.HTTPForbidden()
                self.enqueue("push:" + origin + ":" + key, origin, text)
            except (KeyError, ValueError, TypeError):
                raise web.HTTPBadRequest() from None
            return web.json_response({"status": "queued"}, status=202)

        async def github(request):
            secret = cfg.get("github_secret", "")
            if len(secret) < 24:
                raise web.HTTPNotFound()
            raw = await request.read()
            expected = (
                "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            )
            if not hmac.compare_digest(
                expected, request.headers.get("X-Hub-Signature-256", "")
            ):
                raise web.HTTPUnauthorized()
            try:
                await self.github_event(
                    request.headers.get("X-GitHub-Event", ""),
                    json.loads(raw),
                    request.headers.get("X-GitHub-Delivery", ""),
                )
            except (ValueError, KeyError, TypeError):
                raise web.HTTPBadRequest() from None
            return web.json_response({"status": "accepted"}, status=202)

        app = web.Application(client_max_size=1_000_000)
        app.router.add_post("/push", push)
        app.router.add_post("/github", github)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", int(cfg.get("port", 6186))).start()
