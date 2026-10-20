"""富文本消息的发送和编辑（Bot API 的 sendRichMessage / editMessageText 的 rich_message）。

python-telegram-bot 还没有封装富文本消息，这里用 Bot.do_api_request 直接调接口。
Telegram 拒绝这条富文本时（解析失败等 400），自动改成纯文本再发一次，纯文本按 4096 的上限
在段落边界拆成几条——消息绝不能因为格式问题被吞掉。
"""

import logging
import warnings

from telegram import InlineKeyboardMarkup, Message
from telegram.error import BadRequest
from telegram.warnings import PTBUserWarning

logger = logging.getLogger(__name__)

RICH_CHARS = 30000  # 富文本消息上限 32768 字，留出余量；更长的由调用方先用 render.cut 拆开
PLAIN_CHARS = 4000  # 回退成普通消息时每条的上限（Telegram 4096）


def _harmless(e: BadRequest) -> bool:
    """内容没变之类不需要回退的 400。"""
    return "not modified" in str(e).lower()


def split_plain(text: str, limit: int = PLAIN_CHARS) -> list[str]:
    """纯文本按段落、再按行、最后硬切，拆成不超过 limit 的几段。"""
    text = text.strip() or " "
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n\n", limit // 2, limit)
        if cut == -1:
            cut = text.rfind("\n", limit // 2, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


async def _api(bot, method: str, payload: dict) -> Message:
    with warnings.catch_warnings():
        # PTB 对 editMessageText 走 do_api_request 会提示「请用 edit_message_text」，但它还不支持 rich_message
        warnings.simplefilter("ignore", PTBUserWarning)
        return await bot.do_api_request(method, api_kwargs=payload, return_type=Message)


def _markup(markup: InlineKeyboardMarkup | None) -> dict:
    return {"reply_markup": markup.to_dict()} if markup is not None else {}


async def send_rich(target: Message, html: str, plain: str,
                    markup: InlineKeyboardMarkup | None = None) -> list[Message]:
    """回复 target 发一条富文本消息；被拒绝时改发纯文本（可能拆成几条）。返回发出的消息。"""
    payload = {"chat_id": target.chat_id, "rich_message": {"html": html},
               "reply_parameters": {"message_id": target.message_id, "allow_sending_without_reply": True},
               **_markup(markup)}
    if getattr(target, "is_topic_message", False) and getattr(target, "message_thread_id", None):
        payload["message_thread_id"] = target.message_thread_id
    try:
        return [await _api(target.get_bot(), "sendRichMessage", payload)]
    except BadRequest as e:
        logger.warning("富文本发送失败，改发纯文本：%s", e)
    parts = split_plain(plain)
    sent = []
    for i, part in enumerate(parts):
        sent.append(await target.reply_text(part, reply_markup=markup if i == len(parts) - 1 else None))
    return sent


async def edit_rich(message: Message, html: str, plain: str, reply_to: Message,
                    markup: InlineKeyboardMarkup | None = None) -> list[Message]:
    """把 message 改成这条富文本（占位的纯文本消息也能直接改成富文本）。被拒绝时改成纯文本：
    第一段改进原消息，放不下的部分另发几条回复 reply_to。返回承载内容的全部消息（第一条是 message）。"""
    payload = {"chat_id": message.chat_id, "message_id": message.message_id,
               "rich_message": {"html": html}, **_markup(markup)}
    try:
        await _api(message.get_bot(), "editMessageText", payload)
        return [message]
    except BadRequest as e:
        if _harmless(e):
            return [message]
        logger.warning("富文本编辑失败，改成纯文本：%s", e)
    parts = split_plain(plain)
    try:
        await message.edit_text(parts[0], reply_markup=markup if len(parts) == 1 else None)
    except BadRequest as e:
        if not _harmless(e):
            raise
    sent = [message]
    for i, part in enumerate(parts[1:], 1):
        sent.append(await reply_to.reply_text(part, reply_markup=markup if i == len(parts) - 1 else None))
    return sent
