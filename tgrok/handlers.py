"""普通消息的处理：判断要不要回、拼出对话、生成回复，并记进对话缓存、最近群聊和长期记忆。"""

import logging

from telegram import Message, Update
from telegram.constants import MessageEntityType
from telegram.ext import ContextTypes, filters

from . import config, conversations, media, memory, prompt, recent, viewer
from .chat import stream_reply
from .i18n import t
from .tg_auth import is_authorized

logger = logging.getLogger(__name__)

# 交给 handle_message 的消息类型：文字、带说明的媒体、图片、图片文件、贴纸，以及交给 Gemini 看/听的
# 视频、GIF、圆形视频、语音、音频（命令除外）
MESSAGE_FILTER = (
    filters.TEXT | filters.CAPTION | filters.PHOTO | filters.Document.IMAGE | filters.Sticker.ALL
    | filters.VIDEO | filters.ANIMATION | filters.VIDEO_NOTE | filters.VOICE | filters.AUDIO
    | filters.Document.VIDEO | filters.Document.AUDIO
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


def _join(*parts: str | None) -> str:
    return "\n".join(p for p in parts if p)


def _author_id(m: Message) -> int | None:
    """真正发这条消息的人的 id；频道和匿名管理员发的没有。"""
    return m.from_user.id if m.from_user is not None and m.sender_chat is None else None


def _log_text(m: Message) -> str:
    """记进原始记录的一条消息：媒体标签（预读好了带转述）+ 文字；转发的写明原作者。"""
    entry = media.media_entry(m)
    if entry:
        heard = viewer.ready(entry["uid"])
        what = media.describe(entry)
        tag = t("observed_media_heard", what=what, text=heard) if heard else t("observed_media", what=what)
    elif m.sticker:
        tag = t("observed_sticker", emoji=f" {m.sticker.emoji}" if m.sticker.emoji else "")
    elif media.image_entry(m):
        tag = t("observed_image")
    else:
        tag = ""
    text = " ".join(p for p in (tag, m.text or m.caption) if p)
    origin = getattr(m, "forward_origin", None)
    if text and origin is not None:
        text = t("recent_forward", origin=prompt.origin_label(origin)) + text
    return text


def _observe(msg: Message, job) -> None:
    """旁听群里没叫到 bot 的消息：只存进原始记录，由后台定期整理，不触发模型请求。
    媒体正在预读时等转述好了再记，记录里就有语音、视频的内容。"""
    if not memory.is_observed(msg.chat_id):
        return
    ts = msg.date.timestamp() if msg.date else None

    def write(_job=None) -> None:
        text = _log_text(msg)
        if text:
            memory.observe(msg.chat_id, _author_id(msg), prompt.sender_label(msg), text, msg.message_id, ts)

    if job is not None and not job.done():
        job.add_done_callback(write)
    else:
        write()


def _since(m: Message) -> float:
    return m.date.timestamp() if m.date else 0.0


async def _follow_up(msg: Message, bot, question: str, note: str | None) -> list[dict] | None:
    """回复 bot 的消息（带不带 @ 都算）：接上那条回复对应的对话历史，附上那之后群里新聊的内容。"""
    replied = msg.reply_to_message
    images = await media.image_data_urls(bot, msg)
    if not question and not images and not note:
        return None
    text = prompt.asked_by(msg, _join(question, note) or t("look_image"))
    chat_since = recent.block(msg.chat_id, msg.message_id, since=_since(replied), skip={replied.message_id})
    history = conversations.get(msg.chat_id, replied.message_id)
    if history is not None:
        return history + [prompt.user_turn(text, images, msg.chat_id, question, recent_chat=chat_since)]
    # 历史已过期（如 bot 重启）：把 bot 那条回复并入本轮 user 消息作为最小上下文，
    # 不单独伪造 assistant 轮——以 assistant 开头的对话会被严格交替的接口拒绝
    quoted = replied.text or replied.caption or ""
    if quoted:
        text = t("prev_reply", content=quoted) + "\n\n" + text
    return prompt.new_conversation(prompt.user_turn(
        text, images, msg.chat_id, question, memory_for=[msg.from_user.id],
        recent_chat=recent.block(msg.chat_id, msg.message_id, since=0, skip={replied.message_id})))


def _quoted(msg: Message, replied: Message | None) -> str | None:
    """被引用内容的描述：同一聊天里的消息，或别的聊天/频道里的消息（external_reply）。"""
    quote = msg.quote.text if msg.quote else None
    if replied is not None:
        return prompt.quoted_context(replied, asker=msg.from_user, quote=quote)
    return prompt.external_context(msg.external_reply, quote)


async def _new_question(msg: Message, bot, question: str, note: str | None) -> list[dict] | None:
    """新对话：群里 @bot（可以引用别人的消息）或私聊直接提问。"""
    replied = msg.reply_to_message
    quoted = _quoted(msg, replied)
    images = await media.image_data_urls(bot, msg, replied)
    if not question and not quoted and not images and not note:
        await msg.reply_text(t("nudge"))
        return None
    own_image = bool(images) and not quoted
    text = _join(question, note) or (t("look_image") if own_image else t("comment_default"))
    if quoted:
        text = quoted + "\n\n" + t("question_about_quote", name=prompt.person_label(msg.from_user), question=text)
    else:
        text = prompt.asked_by(msg, text)
    quoted_text = (replied.text or replied.caption or "") if replied else ""
    # 档案附上提问者，以及被引用消息的真正作者（不是 bot 自己、不是频道时）
    author = (_author_id(replied) if quoted and replied is not None and replied.from_user is not None
              and not replied.from_user.is_bot else None)
    people = [msg.from_user.id] + ([author] if author is not None and author != msg.from_user.id else [])
    skip = {replied.message_id} if replied is not None else set()
    return prompt.new_conversation(prompt.user_turn(
        text, images, msg.chat_id, f"{question}\n{quoted_text}", memory_for=people,
        recent_chat=recent.block(msg.chat_id, msg.message_id, since=0, skip=skip)))


def _memory_record(msg: Message, question: str, note: str | None, is_reply_to_bot: bool) -> tuple[str, dict | None]:
    """记进长期记忆的（提问, 被引用的消息）。引用别人的消息时，提问里只写「回复了谁」，
    被引用的原话按它真正的作者单独记，免得以后把别人的话当成提问者说的。"""
    topic = question or note or (t("look_image") if is_reply_to_bot else t("comment_default"))
    replied = msg.reply_to_message
    if replied is None or is_reply_to_bot:
        return topic, None
    who = prompt.sender_label(replied)
    if _author_id(replied) == msg.from_user.id:
        return t("ask_about_own", question=topic), None
    quoted = {"user_id": _author_id(replied), "who": who, "text": _log_text(replied),
              "msg_id": replied.message_id, "ts": _since(replied) or None}
    return t("ask_about_quote", who=who, question=topic), quoted


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or msg.from_user is None:
        return
    bot = context.bot
    # 所有收到的群消息都先记进最近群聊，媒体先在后台预读（别的 bot、频道和匿名管理员发的也算群聊内容）
    recent.add(msg, bot.id)
    job = media.prefetch(bot, msg)
    if msg.from_user.is_bot:
        return
    if config.ENABLE_VISION:
        # 被动记录相册成员（在任何提前 return 之前），供之后回复相册时取整组图片
        media.remember_album(msg)

    replied = msg.reply_to_message
    is_reply_to_bot = bool(replied and replied.from_user and replied.from_user.id == bot.id
                           and replied.sender_chat is None)
    if not (msg.chat.type == "private" or is_reply_to_bot or is_mentioned(msg, bot.username, bot.id)):
        _observe(msg, job)
        return
    if not is_authorized(msg.from_user.id):
        logger.info("静默忽略未授权用户 %s (id=%s)", msg.from_user.full_name, msg.from_user.id)
        _observe(msg, job)
        return

    question = extract_question(msg, bot.username)
    logger.info("收到请求 chat=%s(%s) user=%s(%s) reply_to_bot=%s q=%.80s",
                msg.chat_id, msg.chat.type, msg.from_user.full_name, msg.from_user.id, is_reply_to_bot, question)
    if not is_reply_to_bot:
        media.prefetch(bot, replied)  # 被引用的媒体 bot 之前可能没见过（比如重启前发的）
    # 贴纸、视频语音、看不到的图片：说明和提问一起交给模型，没有文字也要让模型知道对方发了什么
    media.offer_to_watch(bot, msg, replied)
    note = media.media_note(msg)
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
    asker = prompt.person_label(msg.from_user)
    recent.add_bot(msg.chat_id, answer_ids, answer, asker)
    topic, quoted = _memory_record(msg, question, note, is_reply_to_bot)
    memory.record_exchange(msg.chat_id, msg.from_user.id, asker, topic, answer, quoted=quoted, msg_id=msg.message_id)
