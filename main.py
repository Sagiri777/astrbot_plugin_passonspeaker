from __future__ import annotations

import re
from copy import deepcopy

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Reply
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.star.filter.command import GreedyStr


class PassOnSpeakerPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._bound_targets: dict[str, list[str]] = {}

    @staticmethod
    def _is_supported_private_admin(event: AstrMessageEvent) -> bool:
        return event.is_private_chat() and event.is_admin()

    @staticmethod
    def _validate_sid(raw_sid: str) -> str:
        sid = raw_sid.strip()
        MessageSession.from_str(sid)
        return sid

    @staticmethod
    def _parse_sid_list(raw_value: str) -> list[str]:
        normalized = raw_value.replace(",", " ")
        return [item for item in normalized.split() if item]

    @classmethod
    def _validate_sid_list(cls, raw_value: str) -> list[str]:
        validated: list[str] = []
        for item in cls._parse_sid_list(raw_value):
            sid = cls._validate_sid(item)
            if sid not in validated:
                validated.append(sid)
        return validated

    @classmethod
    def _extract_targets_from_text(
        cls,
        text: str,
    ) -> tuple[str, list[str]]:
        match = re.search(r"\s--(?:sid|umo)\s+(.+)$", text.strip())
        if not match:
            return text.strip(), []

        targets = cls._validate_sid_list(match.group(1))
        content = text[: match.start()].rstrip()
        return content, targets

    @staticmethod
    def _format_message_type_label(message_type: str) -> str:
        if message_type == "FriendMessage":
            return "私聊"
        if message_type == "GroupMessage":
            return "群聊"
        return message_type

    async def _describe_sid(self, sid: str) -> str:
        try:
            session = MessageSession.from_str(sid)
        except Exception:
            return sid

        display_name = ""
        try:
            platform_session = await self.context.get_db().get_platform_session_by_id(
                session.session_id
            )
            if platform_session and platform_session.display_name:
                display_name = platform_session.display_name.strip()
        except Exception as exc:
            logger.debug("passonspeaker failed to resolve sid display name: %s", exc)

        session_label = self._format_message_type_label(session.message_type.value)
        if display_name:
            return f"{display_name}（{session_label}，平台 {session.platform_id}）"
        return f"{session_label} {session.session_id}（平台 {session.platform_id}）"

    async def _describe_sid_list(self, sids: list[str]) -> list[str]:
        descriptions: list[str] = []
        for sid in sids:
            descriptions.append(await self._describe_sid(sid))
        return descriptions

    async def _forward_to_sid(
        self,
        source_event: AstrMessageEvent,
        target_sid: str,
        message_chain: MessageChain,
    ) -> tuple[bool, str]:
        if not message_chain.chain:
            return False, "消息内容为空，未执行转发。"

        try:
            validated_sid = self._validate_sid(target_sid)
            sent = await self.context.send_message(validated_sid, message_chain)
        except Exception as exc:
            logger.warning(
                "passonspeaker forward failed. sender=%s sid=%s error=%s",
                source_event.get_sender_id(),
                target_sid,
                exc,
            )
            return False, f"转发失败：{exc}"

        if not sent:
            return False, "转发失败：未找到与该 sid 对应的平台实例。"

        logger.info(
            "passonspeaker forwarded message. sender=%s from=%s to=%s",
            source_event.get_sender_id(),
            source_event.unified_msg_origin,
            validated_sid,
        )
        return True, ""

    async def _forward_to_multiple_sids(
        self,
        source_event: AstrMessageEvent,
        target_sids: list[str],
        message_chain: MessageChain,
    ) -> tuple[list[str], list[str]]:
        succeeded: list[str] = []
        failed: list[str] = []

        for target_sid in target_sids:
            success, error_message = await self._forward_to_sid(
                source_event=source_event,
                target_sid=target_sid,
                message_chain=MessageChain(chain=deepcopy(message_chain.chain)),
            )
            if success:
                succeeded.append(target_sid)
            else:
                failed.append(f"{target_sid} ({error_message})")

        return succeeded, failed

    async def _send_with_feedback(
        self,
        event: AstrMessageEvent,
        target_sids: list[str],
        message_chain: MessageChain,
        success_text: str,
    ):
        succeeded, failed = await self._forward_to_multiple_sids(
            source_event=event,
            target_sids=target_sids,
            message_chain=message_chain,
        )
        if failed:
            result_parts = []
            if succeeded:
                result_parts.append("已转发到：\n" + "\n".join(succeeded))
            result_parts.append("以下目标转发失败：\n" + "\n".join(failed))
            yield event.plain_result("\n".join(result_parts))
            return

        yield event.plain_result(success_text)

    @staticmethod
    def _extract_reply_component(event: AstrMessageEvent) -> Reply | None:
        for component in event.get_messages():
            if isinstance(component, Reply):
                return component
        return None

    def _build_command_forward_chain(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> tuple[MessageChain | None, str | None]:
        normalized = text.strip()
        if normalized:
            return MessageChain().message(normalized), None

        reply_component = self._extract_reply_component(event)
        if not reply_component:
            return None, "请提供要转发的文本，或在命令中引用一条消息。"

        if not reply_component.chain:
            return None, "引用消息内容不可用，暂时无法转发这条被引用消息。"

        return MessageChain(chain=deepcopy(reply_component.chain)), None

    @filter.command_group("passon")
    def passon(self) -> None:
        """私聊转发辅助指令组"""

    @passon.command("bind")
    async def passon_bind(
        self,
        event: AstrMessageEvent,
        raw_sid: GreedyStr = "",
    ):
        if not self._is_supported_private_admin(event):
            yield event.plain_result("仅 AstrBot 管理员可在私聊中使用 /passon。")
            return

        raw_sid = raw_sid.strip()
        if not raw_sid:
            yield event.plain_result("用法：/passon bind <umo1 umo2...>")
            return

        try:
            target_sids = self._validate_sid_list(raw_sid)
        except Exception as exc:
            yield event.plain_result(f"umo 格式不合法：{exc}")
            return

        if not target_sids:
            yield event.plain_result("请至少提供一个合法的 umo。")
            return

        self._bound_targets[event.unified_msg_origin] = target_sids
        descriptions = await self._describe_sid_list(target_sids)
        yield event.plain_result("绑定成功。后续默认可转发到：\n" + "\n".join(descriptions))

    @passon.command("status")
    async def passon_status(self, event: AstrMessageEvent):
        if not self._is_supported_private_admin(event):
            yield event.plain_result("仅 AstrBot 管理员可在私聊中使用 /passon。")
            return

        target_sids = self._bound_targets.get(event.unified_msg_origin, [])
        if target_sids:
            descriptions = await self._describe_sid_list(target_sids)
            yield event.plain_result("当前已绑定默认目标：\n" + "\n".join(descriptions))
            return

        yield event.plain_result("当前未绑定默认目标。")

    @passon.command("unbind")
    async def passon_unbind(self, event: AstrMessageEvent):
        if not self._is_supported_private_admin(event):
            yield event.plain_result("仅 AstrBot 管理员可在私聊中使用 /passon。")
            return

        if self._bound_targets.pop(event.unified_msg_origin, None):
            yield event.plain_result("已解除当前私聊会话的转发绑定。")
            return

        yield event.plain_result("当前没有可解除的转发绑定。")

    @passon.command("send")
    async def passon_send(
        self,
        event: AstrMessageEvent,
        send_args: GreedyStr = "",
    ):
        if not self._is_supported_private_admin(event):
            yield event.plain_result("仅 AstrBot 管理员可在私聊中使用 /passon。")
            return

        send_args = send_args.strip()
        try:
            text, target_sids = self._extract_targets_from_text(send_args)
        except Exception as exc:
            yield event.plain_result(f"umo 格式不合法：{exc}")
            return

        if not target_sids:
            target_sids = self._bound_targets.get(event.unified_msg_origin, [])

        if not target_sids:
            yield event.plain_result(
                "不知道要往哪发。请先使用 /passon bind <umo...> 绑定，"
                "或在消息末尾附加 --umo <umo...>。",
            )
            return

        message_chain, error_message = self._build_command_forward_chain(event, text)
        if not message_chain:
            yield event.plain_result(error_message or "没有可转发的消息内容。")
            return

        async for result in self._send_with_feedback(
            event=event,
            target_sids=target_sids,
            message_chain=message_chain,
            success_text="已完成转发。",
        ):
            yield result
