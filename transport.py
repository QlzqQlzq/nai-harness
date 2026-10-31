"""MaiBot message conversion. All lookups are constrained to the current stream."""
from .runtime import Context
from .state import UserError


def unwrap(value):
    if not isinstance(value, dict) or value.get("success") is False:
        return {}
    message = value.get("message", value)
    return message if isinstance(message, dict) else {}


def parts(message):
    value = message.get("raw_message", [])
    return value if isinstance(value, list) else []


def images_from(message):
    images = []
    for part in parts(message):
        if not isinstance(part, dict) or part.get("type") != "image":
            continue
        binary = part.get("binary_data_base64")
        data = part.get("data")
        if isinstance(binary, str) and binary:
            images.append("base64://" + binary)
        elif isinstance(data, str) and data.startswith(("https://", "http://", "base64://", "data:image/")):
            images.append(data)
    return images


def _mapping(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise UserError("消息上下文格式无效，无法确认用户与会话。")
    return value


def _identifier(*values):
    identifiers = []
    for value in values:
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise UserError("消息身份字段格式无效。")
        text = str(value)
        if text != text.strip() or any(ord(ch) < 32 for ch in text):
            raise UserError("消息身份字段格式无效。")
        identifiers.append(text)
    if len(set(identifiers)) > 1:
        raise UserError("消息身份与会话信息不一致。")
    return identifiers[0] if identifiers else ""


def command_context(payload):
    payload = _mapping(payload)
    message = unwrap(payload.get("message", {}))
    info = _mapping(message.get("message_info"))
    user = _mapping(info.get("user_info"))
    group = _mapping(info.get("group_info"))
    reply = message.get("reply_to") or ""
    if isinstance(reply, dict):
        reply = reply.get("target_message_id", "")
    for part in parts(message):
        if isinstance(part, dict) and part.get("type") == "reply":
            data = part.get("data")
            if isinstance(data, dict):
                reply = data.get("target_message_id") or reply
    return Context(
        stream_id=_identifier(payload.get("stream_id"), message.get("session_id")),
        user_id=_identifier(user.get("user_id"), payload.get("user_id")),
        group_id=_identifier(group.get("group_id"), payload.get("group_id")),
        message_id=_identifier(message.get("message_id"), info.get("message_id")),
        reply_id=_identifier(reply), images=images_from(message),
        platform=_identifier(payload.get("platform"), message.get("platform"), info.get("platform")) or "qq",
    )


class SdkTransport:
    def __init__(self, ctx):
        self.ctx = ctx

    async def _send(self, context, kind, value, quote):
        segments = []
        if quote and context.message_id:
            segments.append({"type": "reply", "data": {"target_message_id": context.message_id}})
        segments.append({"type": kind, "binary_data_base64": value} if kind == "image" else {"type": kind, "data": value})
        return await self.ctx.send.hybrid(segments, context.stream_id, return_details=True)

    async def text(self, context, value, quote):
        return await self._send(context, "text", value, quote)

    async def image(self, context, value, quote):
        return await self._send(context, "image", value, quote)

    async def _lookup(self, context, message_id):
        message = unwrap(await self.ctx.message.get_by_id(
            message_id, stream_id=context.stream_id, include_binary_data=True))
        stream = str(message.get("session_id") or message.get("stream_id") or message.get("chat_id") or "")
        returned_id = message.get("message_id")
        if not message or stream != context.stream_id or (returned_id and str(returned_id) != message_id):
            raise UserError("无法读取当前会话中的目标图片消息。")
        return images_from(message)

    async def images(self, context, quote_first):
        if context.reply_id and quote_first:
            return await self._lookup(context, context.reply_id)
        if context.images:
            return context.images
        if context.message_id:
            attached = await self._lookup(context, context.message_id)
            if attached:
                return attached
        if context.reply_id:
            return await self._lookup(context, context.reply_id)
        return []
