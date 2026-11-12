"""Command-token matching, not substring matching or LLM classification."""

import re
import time

ALIASES = {
    "事件": "events",
    "events": "events",
    "总结": "summary",
    "summary": "summary",
    "帮助": "help",
    "help": "help",
    "滑稽帮助": "help",
    "日程": "schedule",
    "schedule": "schedule",
    "最近日程": "next",
    "next": "next",
    "工单": "tickets",
    "tickets": "tickets",
    "查询": "mc",
    "mc": "mc",
    "公告": "announce",
    "announce": "announce",
    "状态": "status",
    "status": "status",
    "ai": "ai",
    "问": "ai",
}
PATTERN = (
    r"^/?(?:(?:hj|滑稽)(?:\s|$)|(?:" + "|".join(map(re.escape, ALIASES)) + r")(?:\s|$))"
)


def parse_command(text):
    text = text.strip().removeprefix("/")
    parts = text.split(maxsplit=1)
    if not parts:
        return None
    head, tail = parts[0], parts[1] if len(parts) > 1 else ""
    if head in {"hj", "滑稽"}:
        parts = tail.split(maxsplit=1)
        head, tail = (
            (parts[0], parts[1] if len(parts) > 1 else "") if parts else ("帮助", "")
        )
    command = ALIASES.get(head)
    return (command, tail) if command else None


class Gate:
    def __init__(self):
        self.busy = set()
        self.last = {}
        self.seen = {}

    def enter(self, origin, sender, message_id, cooldown):
        now = time.monotonic()
        self.seen = {k: v for k, v in self.seen.items() if now - v < 300}
        self.last = {k: v for k, v in self.last.items() if now - v < max(300, cooldown)}
        key = (origin, message_id)
        if message_id and key in self.seen:
            return False
        if (
            origin in self.busy
            or now - self.last.get((origin, sender), -1e10) < cooldown
        ):
            return False
        if message_id:
            self.seen[key] = now
        self.last[(origin, sender)] = now
        self.busy.add(origin)
        return True

    def leave(self, origin):
        self.busy.discard(origin)
