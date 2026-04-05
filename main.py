from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import (
    At,
    AtAll,
    Face,
    File,
    Forward,
    Image,
    Json,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.utils.quoted_message.onebot_client import OneBotClient


class PassOnSpeakerPlugin(Star):
    _INLINE_AT_PATTERN = re.compile(
        r"(?<![\w@＠])[@＠](?P<target>all|\d{5,20})(?=$|[^\w])",
        re.IGNORECASE,
    )

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
        stripped = text.strip()
        match = re.search(r"(?:^|\s)--(?:sid|umo)\s+(.+)$", stripped)
        if not match:
            return stripped, []

        targets = cls._validate_sid_list(match.group(1))
        content = stripped[: match.start()].rstrip()
        return content, targets

    @staticmethod
    def _extract_subcommand_text(
        event: AstrMessageEvent,
        subcommand: str,
    ) -> str:
        message = re.sub(r"\s+", " ", event.get_message_str().strip())
        pattern = rf"^passon\s+{re.escape(subcommand)}(?:\s+(.*))?$"
        match = re.match(pattern, message)
        if not match:
            return ""
        return (match.group(1) or "").strip()

    @staticmethod
    def _format_message_type_label(message_type: str) -> str:
        if message_type == "FriendMessage":
            return "私聊"
        if message_type == "GroupMessage":
            return "群聊"
        return message_type

    @classmethod
    def _build_message_chain_from_text(cls, text: str) -> MessageChain:
        chain = MessageChain()
        if not text:
            return chain

        last_index = 0
        for match in cls._INLINE_AT_PATTERN.finditer(text):
            start, end = match.span()
            if start > last_index:
                chain.message(text[last_index:start])

            target = match.group("target")
            if target.lower() == "all":
                chain.at_all()
            else:
                chain.at(name=target, qq=target)
            last_index = end

        if last_index < len(text):
            chain.message(text[last_index:])
        return chain

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

    @staticmethod
    def _reply_chain_contains_forward(chain: list[Any] | None) -> bool:
        if not isinstance(chain, list):
            return False
        for component in chain:
            if isinstance(component, (Forward, Node, Nodes)):
                return True
            nested_chain = getattr(component, "chain", None)
            if isinstance(
                component, Reply
            ) and PassOnSpeakerPlugin._reply_chain_contains_forward(nested_chain):
                return True
        return False

    @staticmethod
    def _extract_forward_id_from_message_payload(payload: dict[str, Any] | None) -> str:
        if not isinstance(payload, dict):
            return ""
        data = payload.get("data")
        if isinstance(data, dict):
            payload = data
        segments = payload.get("message") or payload.get("messages") or []
        if not isinstance(segments, list):
            return ""

        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") not in ("forward", "forward_msg", "nodes"):
                continue
            seg_data = segment.get("data", {})
            if not isinstance(seg_data, dict):
                continue
            forward_id = seg_data.get("id") or seg_data.get("message_id")
            if isinstance(forward_id, (str, int)) and str(forward_id).strip():
                return str(forward_id).strip()
        return ""

    @staticmethod
    def _parse_onebot_segment(segment: dict[str, Any]) -> list[Any]:
        if not isinstance(segment, dict):
            return []

        seg_type = segment.get("type")
        seg_data = segment.get("data", {})
        if not isinstance(seg_data, dict):
            seg_data = {}

        if seg_type in ("text", "plain"):
            text = seg_data.get("text")
            return [Plain(text=str(text))] if text is not None else []
        if seg_type == "at":
            qq = seg_data.get("qq")
            if qq == "all":
                return [AtAll()]
            return [At(qq=str(qq), name=seg_data.get("name"))] if qq is not None else []
        if seg_type == "face":
            face_id = seg_data.get("id")
            return [Face(id=int(face_id))] if face_id is not None else []
        if seg_type == "image":
            file_ref = seg_data.get("url") or seg_data.get("file")
            return [Image.fromURL(str(file_ref))] if file_ref else []
        if seg_type == "record":
            file_ref = seg_data.get("url") or seg_data.get("file")
            return [Record(file=str(file_ref))] if file_ref else []
        if seg_type == "video":
            file_ref = seg_data.get("url") or seg_data.get("file")
            return [Video(file=str(file_ref))] if file_ref else []
        if seg_type == "file":
            file_ref = seg_data.get("url") or seg_data.get("file") or ""
            name = (
                seg_data.get("name")
                or seg_data.get("file_name")
                or str(file_ref)
                or "file"
            )
            return [
                File(
                    name=str(name),
                    file=str(file_ref),
                    url=str(seg_data.get("url") or ""),
                )
            ]
        if seg_type == "reply":
            reply_id = seg_data.get("id")
            return [Reply(id=reply_id)] if reply_id is not None else []
        if seg_type in ("forward", "forward_msg"):
            forward_id = seg_data.get("id") or seg_data.get("message_id")
            return [Forward(id=str(forward_id))] if forward_id is not None else []
        if seg_type == "json":
            raw_json = seg_data.get("data")
            if isinstance(raw_json, str) and raw_json.strip():
                try:
                    return [Json(data=json.loads(raw_json.replace("&#44;", ",")))]
                except Exception:
                    return []
        return []

    @classmethod
    def _build_node_from_onebot_payload(
        cls, node_payload: dict[str, Any]
    ) -> Node | None:
        if not isinstance(node_payload, dict):
            return None

        sender = node_payload.get("sender")
        if not isinstance(sender, dict):
            sender = {}
        sender_name = (
            sender.get("nickname")
            or sender.get("card")
            or node_payload.get("nickname")
            or node_payload.get("name")
            or sender.get("user_id")
            or node_payload.get("user_id")
            or "Unknown User"
        )
        sender_id = sender.get("user_id") or node_payload.get("user_id") or "0"

        raw_content = node_payload.get("message") or node_payload.get("content") or []
        content_segments: list[dict[str, Any]] = []
        if isinstance(raw_content, list):
            content_segments = raw_content
        elif isinstance(raw_content, str) and raw_content.strip():
            try:
                parsed_content = json.loads(raw_content)
            except Exception:
                parsed_content = None
            if isinstance(parsed_content, list):
                content_segments = parsed_content
            else:
                content_segments = [{"type": "text", "data": {"text": raw_content}}]

        content: list[Any] = []
        for segment in content_segments:
            seg_type = segment.get("type") if isinstance(segment, dict) else None
            seg_data = segment.get("data", {}) if isinstance(segment, dict) else {}
            if seg_type == "node":
                nested_content = seg_data.get("content")
                nested_sender_id = seg_data.get("user_id") or "0"
                nested_sender_name = seg_data.get("nickname") or "Unknown User"
                if isinstance(nested_content, list):
                    nested_node = cls._build_node_from_onebot_payload(
                        {
                            "sender": {
                                "user_id": nested_sender_id,
                                "nickname": nested_sender_name,
                            },
                            "content": nested_content,
                        }
                    )
                    if nested_node:
                        content.append(nested_node)
                continue
            content.extend(cls._parse_onebot_segment(segment))

        return Node(content=content, name=str(sender_name), uin=str(sender_id))

    async def _build_forward_nodes_from_reply(
        self,
        event: AstrMessageEvent,
        reply_component: Reply,
    ) -> MessageChain | None:
        client = OneBotClient(event)
        reply_id = getattr(reply_component, "id", None)
        reply_id_str = str(reply_id).strip() if reply_id is not None else ""
        if not reply_id_str:
            return None

        message_payload = await client.get_msg(reply_id_str)
        forward_id = self._extract_forward_id_from_message_payload(message_payload)
        if not forward_id:
            return None

        forward_payload = await client.get_forward_msg(forward_id)
        if not isinstance(forward_payload, dict):
            return None

        data = forward_payload.get("data")
        if isinstance(data, dict):
            forward_payload = data
        raw_nodes = (
            forward_payload.get("messages")
            or forward_payload.get("message")
            or forward_payload.get("nodes")
            or forward_payload.get("nodeList")
        )
        if not isinstance(raw_nodes, list):
            return None

        nodes: list[Node] = []
        for raw_node in raw_nodes:
            node = self._build_node_from_onebot_payload(raw_node)
            if node:
                nodes.append(node)

        if not nodes:
            return None
        return MessageChain(chain=[Nodes(nodes=nodes)])

    async def _build_command_forward_chain(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> tuple[MessageChain | None, str | None]:
        normalized = text.strip()
        if normalized:
            return self._build_message_chain_from_text(normalized), None

        reply_component = self._extract_reply_component(event)
        if not reply_component:
            return None, "请提供要转发的文本，或在命令中引用一条消息。"

        if self._reply_chain_contains_forward(reply_component.chain):
            forward_chain = await self._build_forward_nodes_from_reply(
                event, reply_component
            )
            if forward_chain:
                return forward_chain, None

        if not reply_component.chain:
            return None, "引用消息内容不可用，暂时无法转发这条被引用消息。"

        return MessageChain(chain=deepcopy(reply_component.chain)), None

    @filter.command_group("passon")
    def passon(self) -> None:
        """私聊转发辅助指令组"""

    @passon.command("bind")
    async def passon_bind(self, event: AstrMessageEvent):
        if not self._is_supported_private_admin(event):
            yield event.plain_result("仅 AstrBot 管理员可在私聊中使用 /passon。")
            return

        raw_sid = self._extract_subcommand_text(event, "bind")
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
        yield event.plain_result(
            "绑定成功。后续默认可转发到：\n" + "\n".join(descriptions)
        )

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
    async def passon_send(self, event: AstrMessageEvent):
        if not self._is_supported_private_admin(event):
            yield event.plain_result("仅 AstrBot 管理员可在私聊中使用 /passon。")
            return

        send_args = self._extract_subcommand_text(event, "send")
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

        message_chain, error_message = await self._build_command_forward_chain(
            event, text
        )
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
