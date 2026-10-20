"""普通消息的处理：判断要不要回、拼出对话、生成回复，并记进对话缓存和长期记忆。"""

import logging

from telegram import Message, Update
from telegram.constants import MessageEntityType
from telegram.ext import ContextTypes, filters

from . import config, conversations, media, memory, prompt
from .chat import stream_reply
from .i18n import t
from .tg_auth import is_authorized

logger = logging.getLogger(__name__)

# 交给 handle_message 的消息类型：文字、带说明的媒体、图片、图片文件和贴纸（命令除外）
MESSAGE_FILTER = (
    filters.TEXT | filters.CAPTION | filters.PHOTO | filters.Document.IMAGE | filters.Sticker.ALL
) & ~filters.COMMAND


def extract_question(msg: Message, bot_username: str) -> str:
    """去掉文本中对 bot 的 @提及（不分大小写），返回剩余的提问内容。"""
    text = msg.text or msg.caption or ""
    mention = f"@{bot_username}"
    result, lower, needle = [], text.lower(), mention.lower()
    i = 0
    while i < len(text):
        j = lower.find(needle, i)
        if j == -1:
            result.append(text[i:])
            break
        result.append(text[i:j])
        i = j + len(needle)
    return "".join(result).strip()


def is_mentioned(msg: Message, bot_username: str, bot_id: int) -> bool:
    text = msg.text or msg.caption or ""
    entities = list(msg.entities or ()) + list(msg.caption_entities or ())
    for ent in entities:
        if ent.type == MessageEntityType.MENTION:
            mentioned = text[ent.offset : ent.offset + ent.length]
            if mentioned.lower() == f"@{bot_username}".lower():
                return True
        elif ent.type == MessageEntityType.TEXT_MENTION and ent.user and ent.user.id == bot_id:
            return True
    return False


def _clip(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _observe(msg: Message) -> None:
    """旁听群里没叫到 bot 的消息：只存进原始记录，由后台定期整理，不触发模型请求。"""
    if not memory.is_observed(msg.chat_id):
        return
    text = msg.text or msg.caption
    if not text and msg.sticker:
        text = t("observed_sticker", emoji=f" {msg.sticker.emoji}" if msg.sticker.emoji else "")
    elif not text and media.image_entry(msg):
        text = t("observed_image")
    if text:
        memory.observe(msg.chat_id, msg.from_user.id, prompt.person_label(msg.from_user), text)


async def _follow_up(msg: Message, bot, question: str, note: str | None) -> list[dict] | None:
    """回复 bot 的消息（带不带 @ 都算）：接上那条回复对应的对话历史。"""
    replied = msg.reply_to_message
    images = await media.image_data_urls(bot, msg)
    if not question and not images and not note:
        return None
    text = prompt.asked_by(msg, question or note or t("look_image"))
    history = conversations.get(msg.chat_id, replied.message_id)
    if history is not None:
        return history + [prompt.user_turn(text, images, msg.chat_id, question)]
    # 历史已过期（如 bot 重启）：把 bot 那条回复并入本轮 user 消息作为最小上下文，
    # 不单独伪造 assistant 轮——以 assistant 开头的对话会被严格交替的接口拒绝
    quoted = replied.text or replied.caption or ""
    if quoted:
        text = t("prev_reply", content=quoted) + "\n\n" + text
    return prompt.new_conversation(
        prompt.user_turn(text, images, msg.chat_id, question, memory_for=[msg.from_user.id]))


async def _new_question(msg: Message, bot, question: str, note: str | None) -> list[dict] | None:
    """新对话：群里 @bot（可以引用别人的消息）或私聊直接提问。"""
    replied = msg.reply_to_message
    quoted = prompt.quoted_context(replied) if replied else None
    images = await media.image_data_urls(bot, msg, replied)
    if not question and not quoted and not images and not note:
        await msg.reply_text(t("nudge"))
        return None
    own_image = bool(images) and not quoted
    text = question or note or (t("look_image") if own_image else t("comment_default"))
    if quoted:
        text = quoted + "\n\n" + t("question_from", name=prompt.person_label(msg.from_user), question=text)
    else:
        text = prompt.asked_by(msg, text)
    quoted_text = (replied.text or replied.caption or "") if replied else ""
    # 档案附上提问者，以及被引用消息的作者（不是 bot 自己时）
    people = [msg.from_user.id] + ([replied.from_user.id] if quoted and replied.from_user
                                   and not replied.from_user.is_bot else [])
    return prompt.new_conversation(
        prompt.user_turn(text, images, msg.chat_id, f"{question}\n{quoted_text}", memory_for=people))


def _memory_topic(msg: Message, question: str, note: str | None, is_reply_to_bot: bool) -> str:
    """记进长期记忆的「问题」：引用别人的消息时带上一小段被引用内容，否则只有一句「怎么看」。"""
    topic = question or note or (t("look_image") if is_reply_to_bot else t("comment_default"))
    replied = msg.reply_to_message
    if replied and not is_reply_to_bot and (replied.text or replied.caption):
        topic = f"{topic}（{_clip(replied.text or replied.caption)}）"
    return topic


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or msg.from_user is None or msg.from_user.is_bot:
        return
    if config.ENABLE_VISION:
        # 被动记录相册成员（在任何提前 return 之前），供之后回复相册时取整组图片
        media.remember_album(msg)

    bot = context.bot
    replied = msg.reply_to_message
    is_reply_to_bot = bool(replied and replied.from_user and replied.from_user.id == bot.id)
    if not (msg.chat.type == "private" or is_reply_to_bot or is_mentioned(msg, bot.username, bot.id)):
        _observe(msg)
        return
    if not is_authorized(msg.from_user.id):
        logger.info("静默忽略未授权用户 %s (id=%s)", msg.from_user.full_name, msg.from_user.id)
        _observe(msg)
        return

    question = extract_question(msg, bot.username)
    logger.info("收到请求 chat=%s(%s) user=%s(%s) reply_to_bot=%s q=%.80s",
                msg.chat_id, msg.chat.type, msg.from_user.full_name, msg.from_user.id, is_reply_to_bot, question)
    note = media.media_note(msg)  # 贴纸、看不到的图片：没有文字也要让模型知道对方发了什么
    if is_reply_to_bot:
        history = await _follow_up(msg, bot, question, note)
    else:
        history = await _new_question(msg, bot, question, note)
    if history is None:
        return

    history = conversations.trim(history)
    answer_ids, answer = await stream_reply(msg, history)
    if not (answer_ids and answer):
        logger.warning("未产生回复 chat=%s user=%s", msg.chat_id, msg.from_user.id)
        return
    logger.info("已回复 chat=%s msg_ids=%s len=%d", msg.chat_id, answer_ids, len(answer))
    full = history + [{"role": "assistant", "content": answer}]
    for message_id in answer_ids:
        conversations.remember(msg.chat_id, message_id, full)
    memory.record_exchange(msg.chat_id, msg.from_user.id, prompt.person_label(msg.from_user),
                           _memory_topic(msg, question, note, is_reply_to_bot), answer)
