"""Reserved extension contract. No provider, network call or model dependency."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AIRequest:
    platform_id: str
    group_id: str
    sender_id: str
    text: str


class AIPort(Protocol):
    async def respond(self, request: AIRequest) -> str: ...


class DisabledAI:
    async def respond(self, request: AIRequest) -> str:
        return "AI 接口尚未接入；当前仅提供插件功能。"
