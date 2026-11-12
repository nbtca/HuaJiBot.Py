"""Original ServerlessMQ JSON protocol and NSQ v2 transport, without credentials in logs."""

import asyncio
import json
import ssl
import struct
import uuid
from contextlib import suppress

import aiohttp

SOURCE = {
    "display_name": "QQGroup",
    "name": "HuaJiBot.Py.MessageBridge",
    "version": "0.2.0",
}
EVENT_TYPES = {
    "player_chat": "Chat",
    "player_join": "JoinLeft",
    "player_quit": "JoinLeft",
    "player_death": "PlayerDeath",
    "player_achievement": "PlayerAchievement",
}


def packet(kind, data):
    return {"type": kind, "source": SOURCE, "data": data}


def minecraft_message(value):
    kind, data = value.get("type"), value.get("data", {})
    source = (value.get("source") or {}).get("display_name", "Unknown")
    name = data.get("player_name", "?")
    if kind == "player_chat":
        return f"[{source}] <{name}> {data.get('message', '')}"
    if kind in {"player_join", "player_quit"}:
        return (
            f"[{source}] {name} {'加入了' if kind == 'player_join' else '离开了'}服务器"
        )
    if kind == "player_death":
        return f"[{source}] {data.get('death_message', '')}"
    if kind == "player_achievement":
        return f"[{source}] {name} 完成了进度 {data.get('name', '')} ({','.join(data.get('criteria', []))})\n{data.get('description', '')}"
    return None


class MQClient:
    def __init__(self, http, config, receive, log):
        self.http, self.config, self.receive, self.log = http, config, receive, log
        self.socket = None
        self.pending = {}

    async def run(self):
        delay = 1
        while True:
            try:
                headers = {
                    "Authorization": f"Bearer {self.config.get('token', '')}",
                    "client-type": "IM",
                    "client-subtype": "QQ",
                    "client-name": SOURCE["name"],
                    "client-version": SOURCE["version"],
                    "address": self.config["address"],
                }
                async with self.http.ws_connect(
                    self.config["address"],
                    headers=headers,
                    heartbeat=30,
                    max_msg_size=2_000_000,
                ) as ws:
                    self.socket, delay = ws, 1
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.ERROR:
                            raise ConnectionError("MQ websocket error")
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            value = json.loads(msg.data)
                            if not isinstance(value, dict):
                                raise ValueError("Invalid MQ packet")
                            if value.get("type") == "get_player_list_response":
                                key = value.get("data", {}).get("request_id")
                                if key in self.pending:
                                    if len(self.pending[key]) < 100:
                                        self.pending[key].append(value)
                            else:
                                await self.receive(value)
                        except (ValueError, TypeError, KeyError) as exc:
                            self.log(f"MQ packet rejected: {type(exc).__name__}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"MQ reconnect: {type(exc).__name__}")
            finally:
                self.socket = None
            await asyncio.sleep(delay)
            delay = min(60, delay * 2)

    async def send(self, value):
        if self.socket is None or self.socket.closed:
            raise ConnectionError("MQ disconnected")
        await self.socket.send_json(value)

    async def query(self, timeout=5):
        key = uuid.uuid4().hex
        self.pending[key] = []
        try:
            await self.send(packet("get_player_list_request", {"request_id": key}))
            await asyncio.sleep(timeout)
            return self.pending[key]
        finally:
            self.pending.pop(key, None)


async def read_nsq_frame(reader):
    size = struct.unpack(">I", await reader.readexactly(4))[0]
    if not 4 <= size <= 2_000_000:
        raise ValueError("NSQ frame size invalid")
    raw = await reader.readexactly(size)
    return struct.unpack(">I", raw[:4])[0], raw[4:]


class NSQConsumer:
    def __init__(self, config, receive, log):
        self.config, self.receive, self.log = config, receive, log

    async def command(self, writer, name, body=None):
        writer.write(name.encode() + b"\n")
        if body is not None:
            writer.write(struct.pack(">I", len(body)) + body)
        await writer.drain()

    async def response(self, reader, writer):
        while True:
            kind, raw = await asyncio.wait_for(read_nsq_frame(reader), 65)
            if kind == 0 and raw == b"_heartbeat_":
                await self.command(writer, "NOP")
                continue
            if kind != 0:
                raise ConnectionError("NSQ handshake rejected")
            return raw

    async def consume(self):
        cfg = self.config
        host, sep, port = cfg["address"].rpartition(":")
        if not sep or not host:
            raise ValueError("NSQ address must be host:port")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port)), 15
        )
        try:
            writer.write(b"  V2")
            identify = {
                "client_id": "huajibot-py",
                "hostname": "huajibot-py",
                "feature_negotiation": True,
                "heartbeat_interval": 30000,
                "tls_v1": bool(cfg.get("tls", False)),
            }
            await self.command(writer, "IDENTIFY", json.dumps(identify).encode())
            raw = await self.response(reader, writer)
            features = json.loads(raw) if raw.startswith(b"{") else {}
            if cfg.get("tls", False):
                if not features.get("tls_v1"):
                    raise ConnectionError("NSQ TLS unavailable")
                await writer.start_tls(
                    ssl.create_default_context(), server_hostname=host
                )
                await self.response(reader, writer)
            if features.get("auth_required"):
                await self.command(writer, "AUTH", cfg.get("secret", "").encode())
                await self.response(reader, writer)
            for field in ("topic", "channel"):
                import re

                if not re.fullmatch(
                    r"[.a-zA-Z0-9_-]{1,64}(?:#ephemeral)?", cfg.get(field, "")
                ):
                    raise ValueError("NSQ topic/channel invalid")
            await self.command(writer, f"SUB {cfg['topic']} {cfg['channel']}")
            await self.response(reader, writer)
            await self.command(writer, "RDY 1")
            while True:
                kind, raw = await asyncio.wait_for(read_nsq_frame(reader), 65)
                if kind == 0 and raw == b"_heartbeat_":
                    await self.command(writer, "NOP")
                elif kind == 1:
                    raise ConnectionError("NSQ error frame")
                elif kind == 2:
                    if len(raw) < 26:
                        raise ValueError("Invalid NSQ message")
                    message_id = raw[10:26].decode("ascii")
                    if not all(c in "0123456789abcdefABCDEF" for c in message_id):
                        raise ValueError("Invalid NSQ message ID")
                    try:
                        await self.receive(message_id, json.loads(raw[26:]))
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.log(f"NSQ retry: {type(exc).__name__}")
                        await self.command(writer, f"REQ {message_id} 5000")
                    else:
                        # Persist notifications before acknowledging the queue message.
                        await self.command(writer, f"FIN {message_id}")
                    await self.command(writer, "RDY 1")
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def run(self):
        delay = 1
        while True:
            try:
                await self.consume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"NSQ reconnect: {type(exc).__name__}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
