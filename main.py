import asyncio
import time
from datetime import datetime, timedelta

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .business import ticket_digest
from .configuration import normalize
from .routing import PATTERN, Gate, parse_command
from .runtime import Runtime
from .services import TZ, Services

HELP = """滑稽社团助手 v0.2
事件 [加入|聊天|死亡|进度] [开|关]：查看／管理游戏事件
总结 [今天|昨天|前天]：默认昨天，需开启记录
日程 [周数] / schedule [周数]：默认未来一周，支持过去周数
最近日程 / next：未来 30 天最近一项
工单 / tickets：进行中的维修工单
查询 / mc：Minecraft 在线状态
公告 内容：管理员发布本群公告
问 问题 / ai 问题：调用当前会话模型
状态：查看插件状态
可直接发送，也可加 / 或 hj 前缀。普通聊天中包含这些词不会触发。
无需 @ 的前提是 QQ 平台向机器人下发普通群消息。"""


class HuajiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = normalize(config)
        self.services = Services(self.config)
        self.gate = Gate()
        self.runtime = None

    async def initialize(self):
        await self.services.start()

        async def send(origin, text):
            return await self.context.send_message(origin, MessageChain().message(text))

        async def llm(origin, text, prompt):
            provider = await self.context.get_current_chat_provider_id(umo=origin)
            response = await self.context.llm_generate(
                chat_provider_id=provider, prompt=text, system_prompt=prompt
            )
            return response.completion_text or ""

        self.runtime = Runtime(
            self.config,
            self.services,
            StarTools.get_data_dir("astrbot_plugin_huaji"),
            send,
            llm,
            logger.warning,
        )
        try:
            await self.runtime.start()
        except BaseException:
            self.runtime = None
            await self.services.close()
            raise
        logger.info("滑稽插件已启动：命令关键词匹配，不启用随机回复。")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=200)
    async def observe(self, event: AstrMessageEvent):
        if (
            event.get_platform_name() not in {"qq_official", "qq_official_webhook"}
            or not self.runtime
        ):
            return
        if not self.runtime.allowed(event.unified_msg_origin):
            return
        if not event.is_at_or_wake_command:
            event.should_call_llm(False)
        sender = str(event.get_sender_id())
        raw = getattr(event.message_obj, "raw_message", None)
        author = (
            raw.get("author", {})
            if isinstance(raw, dict)
            else getattr(raw, "author", None)
        )
        bot_flag = (
            author.get("bot", False)
            if isinstance(author, dict)
            else getattr(author, "bot", False)
        )
        try:
            await self.runtime.observe(
                event.unified_msg_origin,
                sender,
                event.get_sender_name(),
                event.message_str,
                str(getattr(event.message_obj, "message_id", "") or ""),
                float(getattr(event.message_obj, "timestamp", None) or time.time()),
                sender == str(event.get_self_id())
                or bool(bot_flag)
                or sender in self.config.get("ignored_bot_ids", []),
            )
        except Exception as exc:
            logger.warning(f"滑稽后台消息处理失败：{type(exc).__name__}")

    @filter.regex(PATTERN, priority=100)
    async def on_command(self, event: AstrMessageEvent):
        """复用 HuaJiBot 的命令词和别名；命中后才执行业务。"""
        if event.get_platform_name() not in {"qq_official", "qq_official_webhook"}:
            return
        parsed = parse_command(event.message_str)
        if parsed is None:
            return
        origin = event.unified_msg_origin
        # Empty allowlist means no access. Use UMO, never a display QQ group number.
        if origin not in self.config.get("allowed_sessions", []):
            event.stop_event()
            return
        if str(event.get_sender_id()) == str(event.get_self_id()):
            event.stop_event()
            return
        event.should_call_llm(False)
        event.stop_event()
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        if not self.gate.enter(
            origin,
            event.get_sender_id(),
            message_id,
            max(0, int(self.config.get("cooldown_seconds", 10))),
        ):
            return
        try:
            command, argument = parsed
            timeout = (
                self.config["summary"]["timeout_seconds"]
                if command == "summary"
                else 90
            )
            async with asyncio.timeout(timeout):
                result = await self.dispatch(command, argument, event)
            # Send before returning, so stop_event cannot suppress the response.
            if len(result) > 15000:
                result = "结果超过单次回复上限，请缩小查询范围或提高工单查询起始日期。"
            for offset in range(0, len(result), 3000):
                await event.send(event.plain_result(result[offset : offset + 3000]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate third-party provider failures
            # Do not log URLs, tokens, prompts or exception bodies.
            logger.warning(f"滑稽插件命令失败：{type(exc).__name__}")
            await event.send(
                event.plain_result("服务暂时不可用，请稍后再试或联系管理员检查配置。")
            )
        finally:
            self.gate.leave(origin)

    async def dispatch(self, command, argument, event):
        if command == "help":
            return HELP
        if command == "status":
            return "滑稽插件 v0.2.0\n命令关键词触发；随机回复关闭\n通知队列：" + str(
                self.runtime.store.counts() if self.runtime else {}
            )
        if command == "schedule":
            try:
                weeks = int(argument or "1")
            except ValueError:
                return "用法：日程 [整数周数]，如 日程 2 或 日程 -1"
            if not -128 <= weeks <= 48:
                return "周数超出范围 [-128,48]。"
            return await self.services.calendar(weeks * 7)
        if command == "next":
            return (
                await self.services.calendar(30, only_next=True)
                if not argument
                else "用法：最近日程"
            )
        if command == "tickets":
            if not self.config.get("tickets_enabled"):
                return "工单查询未启用。"
            return (
                ticket_digest(await self.runtime.tickets(), datetime.now(TZ))
                if not argument
                else "用法：工单"
            )
        if command == "mc":
            if argument:
                return "用法：查询"
            result = await self.runtime.players(event.unified_msg_origin)
            return result if result is not None else await self.services.minecraft()
        if command == "events":
            return self.events(argument, event)
        if command == "summary":
            cfg = self.config["summary"]
            if not cfg["enabled"] or event.unified_msg_origin not in cfg["sessions"]:
                return "本群尚未开启聊天记录与日报功能。"
            days = {"": 1, "昨天": 1, "今天": 0, "前天": 2}
            if argument not in days:
                return "用法：总结 [今天|昨天|前天]"
            result = await self.runtime.summary.generate(
                event.unified_msg_origin,
                datetime.now(TZ).date() - timedelta(days=days[argument]),
            )
            return result or "该日消息数量不足，未生成总结。"
        if command == "announce":
            if str(event.get_sender_id()) not in self.config.get("admin_ids", []):
                return "只有配置的机器人管理员可以发布公告。"
            return (
                "【群公告】\n" + argument[:1500]
                if argument
                else "用法：公告 内容（发送普通消息，不修改 QQ 群公告栏）"
            )
        if command == "ai":
            if not self.config.get("ai_enabled", True):
                return "AI 入口已关闭。"
            if not argument:
                return "用法：问 问题"
            if len(argument) > 2000:
                return "问题请限制在 2000 字以内。"
            provider = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
            reply = await self.context.llm_generate(
                chat_provider_id=provider,
                prompt=argument,
                system_prompt=self.config.get("ai_system_prompt", "请用简洁中文回答。"),
            )
            return reply.completion_text or "模型未返回文本。"
        return HELP

    def events(self, argument, event):
        types = {
            "加入": "JoinLeft",
            "聊天": "Chat",
            "死亡": "PlayerDeath",
            "进度": "PlayerAchievement",
        }
        origin = event.unified_msg_origin
        rules = [
            r
            for c in self.config["minecraft_clients"]
            for r in c.get("groups", [])
            if r.get("session") == origin
        ]
        if not rules:
            return "本群未配置游戏消息桥接。"
        disabled = self.runtime.store.get(
            "mc-events:" + origin, rules[0].get("disabled_events", [])
        )
        if not argument:
            return "\n".join(
                f"{name}：{'关' if value in disabled else '开'}"
                for name, value in types.items()
            )
        if str(event.get_sender_id()) not in self.config.get("admin_ids", []):
            return "只有机器人管理员可以修改事件开关。"
        args = argument.split()
        if (
            len(args) != 2
            or args[0] not in types
            or args[1].lower()
            not in {"开", "开启", "on", "true", "关", "关闭", "off", "false"}
        ):
            return "用法：事件 加入|聊天|死亡|进度 开|关"
        value, disabled = types[args[0]], set(disabled)
        if args[1].lower() in {"开", "开启", "on", "true"}:
            disabled.discard(value)
        else:
            disabled.add(value)
        self.runtime.store.set("mc-events:" + origin, sorted(disabled))
        return "事件开关已保存。"

    @filter.llm_tool(name="huaji_calendar")
    async def calendar_tool(self, event: AstrMessageEvent, weeks: int = 1) -> str:
        """查询社团日程；此工具只读，不创建或修改日程。

        Args:
            weeks(number): 要查询的周数，负数查询过去，范围 -128 到 48。
        """
        if not self.runtime or not self.runtime.allowed(event.unified_msg_origin):
            return "该会话未授权。"
        if not isinstance(weeks, int) or not -128 <= weeks <= 48:
            return "周数超出范围。"
        return await self.services.calendar(weeks * 7)

    @filter.llm_tool(name="huaji_query")
    async def query_tool(self, event: AstrMessageEvent, kind: str) -> str:
        """查询维修工单或游戏服务器玩家；不会更改工单和服务器。

        Args:
            kind(string): tickets 查询维修工单，players 查询在线玩家。
        """
        if not self.runtime or not self.runtime.allowed(event.unified_msg_origin):
            return "该会话未授权。"
        if kind == "tickets" and self.config.get("tickets_enabled"):
            return ticket_digest(await self.runtime.tickets(), datetime.now(TZ))
        if kind == "players":
            return (
                await self.runtime.players(event.unified_msg_origin)
                or await self.services.minecraft()
            )
        return "功能未启用或类型不支持。"

    async def terminate(self):
        if self.runtime:
            await self.runtime.close()
            self.runtime = None
        await self.services.close()
