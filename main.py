from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.message_session import MessageSession


@dataclass
class PendingForward:
    message_chain: MessageChain
    default_target_sids: list[str]
    preview_text: str


@register(
    "astrbot_plugin_passonspeaker",
    "guozimier",
    "Forward admin private messages to a target sid using the bot account.",
    "v1.0.0",
)
class PassOnSpeakerPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._bound_targets: dict[str, list[str]] = {}
        self._pending_messages: dict[str, PendingForward] = {}

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
    def _clone_forward_chain(event: AstrMessageEvent) -> MessageChain:
        return MessageChain(chain=deepcopy(event.get_messages()))

    @staticmethod
    def _strip_plain_suffix(
        chain: MessageChain,
        text_to_remove: str,
    ) -> MessageChain:
        if not chain.chain:
            return chain

        first_component = chain.chain[0]
        if not isinstance(first_component, Plain):
            return chain

        text = first_component.text
        if not text.endswith(text_to_remove):
            return chain

        remaining = text[: -len(text_to_remove)].rstrip()
        if remaining:
            first_component.text = remaining
        else:
            chain.chain.pop(0)
        return chain

    @staticmethod
    def _get_preview_text(event: AstrMessageEvent) -> str:
        preview = event.message_str.strip()
        if preview:
            return preview
        return event.get_message_outline().strip() or "[非文本消息]"

    @staticmethod
    def _is_confirm_text(text: str) -> bool:
        normalized = text.strip().lower()
        return normalized in {
            "y",
            "yes",
            "ok",
            "sure",
            "confirm",
            "1",
            "是",
            "好",
            "好的",
            "确认",
            "要",
        }

    @staticmethod
    def _is_cancel_text(text: str) -> bool:
        normalized = text.strip().lower()
        return normalized in {
            "n",
            "no",
            "cancel",
            "0",
            "否",
            "不",
            "不用",
            "取消",
        }

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

    async def _handle_pending_confirmation(self, event: AstrMessageEvent):
        pending = self._pending_messages.get(event.unified_msg_origin)
        if not pending:
            return

        reply_text = event.message_str.strip()
        if self._is_confirm_text(reply_text):
            self._pending_messages.pop(event.unified_msg_origin, None)
            async for result in self._send_with_feedback(
                event=event,
                target_sids=pending.default_target_sids,
                message_chain=pending.message_chain,
                success_text="已转发到默认目标。",
            ):
                yield result
            return

        if self._is_cancel_text(reply_text):
            self._pending_messages.pop(event.unified_msg_origin, None)
            yield event.plain_result("已取消这次转发。")
            return

        try:
            target_sids = self._validate_sid_list(reply_text)
        except Exception as exc:
            yield event.plain_result(f"这条消息既不是确认，也不是合法 sid 列表：{exc}")
            return

        if not target_sids:
            yield event.plain_result(
                "请回复 y/是 确认，回复 n/否 取消，或发送一个/多个 sid。"
            )
            return

        self._pending_messages.pop(event.unified_msg_origin, None)
        async for result in self._send_with_feedback(
            event=event,
            target_sids=target_sids,
            message_chain=pending.message_chain,
            success_text="已按你刚提供的目标完成转发。",
        ):
            yield result

    @filter.command("passon")
    async def passon_command(
        self,
        event: AstrMessageEvent,
        args_str: str = "",
    ):
        if not self._is_supported_private_admin(event):
            yield event.plain_result("仅 AstrBot 管理员可在私聊中使用 /passon。")
            return

        args = args_str.strip()
        if not args:
            yield event.plain_result(
                "用法：/passon bind <sid...> 绑定默认目标；"
                "/passon unbind 解除绑定；"
                "/passon status 查看当前绑定；"
                "/passon <文本> --sid <sid1 sid2...> 临时转发。",
            )
            return

        if args == "status":
            target_sids = self._bound_targets.get(event.unified_msg_origin, [])
            if target_sids:
                descriptions = await self._describe_sid_list(target_sids)
                yield event.plain_result(
                    "当前已绑定默认目标：\n" + "\n".join(descriptions)
                )
            else:
                yield event.plain_result("当前未绑定默认目标。")
            return

        if args == "unbind":
            self._pending_messages.pop(event.unified_msg_origin, None)
            if self._bound_targets.pop(event.unified_msg_origin, None):
                yield event.plain_result("已解除当前私聊会话的转发绑定。")
            else:
                yield event.plain_result("当前没有可解除的转发绑定。")
            return

        if args.startswith("bind "):
            raw_sid = args[5:].strip()
            if not raw_sid:
                yield event.plain_result("用法：/passon bind <sid1 sid2...>")
                return

            try:
                target_sids = self._validate_sid_list(raw_sid)
            except Exception as exc:
                yield event.plain_result(f"sid 格式不合法：{exc}")
                return

            if not target_sids:
                yield event.plain_result("请至少提供一个合法的 sid。")
                return

            self._bound_targets[event.unified_msg_origin] = target_sids
            descriptions = await self._describe_sid_list(target_sids)
            yield event.plain_result(
                "绑定成功。后续默认可转发到：\n" + "\n".join(descriptions)
            )
            return

        try:
            text, target_sids = self._extract_targets_from_text(args)
        except Exception as exc:
            yield event.plain_result(f"sid 格式不合法：{exc}")
            return

        if not text.strip():
            yield event.plain_result("用法：/passon <文本> --sid <sid1 sid2...>")
            return

        if not target_sids:
            target_sids = self._bound_targets.get(event.unified_msg_origin, [])

        if not target_sids:
            yield event.plain_result(
                "不知道要往哪发。请先使用 /passon bind <sid...> 绑定，"
                "或在消息末尾附加 --sid <sid...>。",
            )
            return

        async for result in self._send_with_feedback(
            event=event,
            target_sids=target_sids,
            message_chain=MessageChain().message(text.strip()),
            success_text="已完成转发。",
        ):
            yield result

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def forward_bound_messages(self, event: AstrMessageEvent):
        if not self._is_supported_private_admin(event):
            return

        message_text = event.message_str.strip()
        if message_text.startswith("/passon"):
            return

        if message_text.startswith("/"):
            return

        pending = self._pending_messages.get(event.unified_msg_origin)
        if pending:
            async for result in self._handle_pending_confirmation(event):
                yield result
            return

        try:
            content_text, inline_target_sids = self._extract_targets_from_text(
                message_text
            )
        except Exception as exc:
            yield event.plain_result(f"sid 格式不合法：{exc}")
            return

        message_chain = self._clone_forward_chain(event)
        if inline_target_sids and content_text != message_text:
            suffix_text = message_text[len(content_text) :]
            if suffix_text.strip():
                message_chain = self._strip_plain_suffix(message_chain, suffix_text)
            async for result in self._send_with_feedback(
                event=event,
                target_sids=inline_target_sids,
                message_chain=message_chain,
                success_text="已按消息附加的目标完成转发。",
            ):
                yield result
            return

        default_target_sids = self._bound_targets.get(event.unified_msg_origin, [])
        if not default_target_sids:
            yield event.plain_result(
                "不知道要往哪发。请先使用 /passon bind <sid...> 绑定，"
                "或在消息末尾附加 --sid <sid...>。",
            )
            return

        self._pending_messages[event.unified_msg_origin] = PendingForward(
            message_chain=message_chain,
            default_target_sids=default_target_sids,
            preview_text=self._get_preview_text(event),
        )
        descriptions = await self._describe_sid_list(default_target_sids)
        yield event.plain_result(
            "检测到这条消息没有附加目标。\n"
            f"待转发内容：{self._get_preview_text(event)}\n"
            "是否转发到默认目标：\n"
            + "\n".join(descriptions)
            + "\n回复 y/是 确认；回复 n/否 取消；"
            "也可以直接发送一个或多个 sid 作为本次转发目标。"
        )
