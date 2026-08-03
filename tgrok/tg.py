"""Telegram 接入层：消息路由、图片/相册、管理命令与应用入口。"""

import base64
import logging
from collections import OrderedDict

from telegram import BotCommand, Message, Update
from telegram.constants import MessageEntityType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config
from .config import (
    ADMIN_USER_IDS, ALBUM_CACHE_SIZE, BOT_TOKEN, CONVERSATION_CACHE_SIZE,
    ENABLE_VISION, LLM_BASE_URL, LLM_MODEL, MAX_HISTORY, MAX_IMAGE_BYTES,
)
from .chat import on_cancel_button, stream_reply
from .i18n import t
from .prompt import SYSTEM_PROMPT, with_time
from .tg_auth import allowed_users, is_admin, is_authorized, save_allowed_users

logger = logging.getLogger(__name__)

# 对话历史：key = (chat_id, bot 回复消息的 message_id)，value = OpenAI 格式的 messages 列表。
# 用户回复 bot 的某条消息时，就能接上那条消息对应的上下文继续聊。
conversations: "OrderedDict[tuple[int, int], list[dict]]" = OrderedDict()



def remember(chat_id: int, message_id: int, history: list[dict]) -> None:
    conversations[(chat_id, message_id)] = history
    while len(conversations) > CONVERSATION_CACHE_SIZE:
        conversations.popitem(last=False)


def trim_history(history: list[dict]) -> list[dict]:
    """保留 system 消息 + 最近 MAX_HISTORY 条对话。"""
    if len(history) <= MAX_HISTORY + 1:
        return history
    return [history[0]] + history[-MAX_HISTORY:]

def extract_question(msg: Message, bot_username: str) -> str:
    """去掉文本中对 bot 的 @提及，返回剩余的提问内容。"""
    text = msg.text or msg.caption or ""
    mention = f"@{bot_username}"
    # 大小写不敏感地移除所有提及
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


def quoted_context(msg: Message) -> str | None:
    """如果该消息引用了别人的消息，返回一段描述引用内容的文本。"""
    replied = msg.reply_to_message
    if replied is None:
        return None
    content = replied.text or replied.caption
    if not content:
        return None
    author = replied.from_user.full_name if replied.from_user else t("someone")
    return t("quoted_msg", author=author, content=content)


# 相册缓存：Telegram 的多图消息（相册）是多条独立消息，仅靠 media_group_id 关联，
# 回复相册时 reply_to_message 只指向第一条。bot 收到相册成员消息时先记下
# file_id，之后有人回复相册提问，就能按组取出全部图片。
# key = (chat_id, media_group_id)，value = [{file_id, mime, message_id}]
album_cache: "OrderedDict[tuple[int, str], list[dict]]" = OrderedDict()


def _msg_image_entry(m: Message) -> dict | None:
    """从单条消息提取图片引用（压缩照片取最大尺寸；图片文件校验大小）。"""
    if m.photo:
        return {"file_id": m.photo[-1].file_id, "mime": "image/jpeg", "message_id": m.message_id}
    if m.document and (m.document.mime_type or "").startswith("image/"):
        if m.document.file_size and m.document.file_size > MAX_IMAGE_BYTES:
            return None
        return {"file_id": m.document.file_id, "mime": m.document.mime_type, "message_id": m.message_id}
    return None


def remember_album(msg: Message) -> None:
    """记录相册成员消息的图片引用（被动收集，与是否 @bot 无关）。"""
    if not msg.media_group_id:
        return
    entry = _msg_image_entry(msg)
    if entry is None:
        return
    key = (msg.chat_id, msg.media_group_id)
    group = album_cache.setdefault(key, [])
    if all(e["message_id"] != entry["message_id"] for e in group):
        group.append(entry)
    album_cache.move_to_end(key)
    while len(album_cache) > ALBUM_CACHE_SIZE:
        album_cache.popitem(last=False)


def _image_refs(m: Message) -> list[dict]:
    """取一条消息关联的全部图片引用：相册成员展开为整组，普通消息取自身。"""
    if m.media_group_id:
        group = album_cache.get((m.chat_id, m.media_group_id))
        if group:
            return sorted(group, key=lambda e: e["message_id"])
    entry = _msg_image_entry(m)
    return [entry] if entry else []


async def image_data_urls(bot, *messages: Message | None) -> list[str]:
    """提取消息中的图片（相册自动展开为整组），转为 base64 data URL。"""
    refs, seen = [], set()
    for m in messages:
        if m is None:
            continue
        for entry in _image_refs(m):
            if entry["file_id"] not in seen:
                seen.add(entry["file_id"])
                refs.append(entry)
    urls = []
    for entry in refs[:config.MAX_IMAGES]:
        try:
            file = await bot.get_file(entry["file_id"])
            data = bytes(await file.download_as_bytearray())
        except Exception:
            logger.exception("下载图片失败 file_id=%s", entry["file_id"])
            continue
        urls.append(f"data:{entry['mime']};base64," + base64.b64encode(data).decode())
    return urls


def build_content(text: str, images: list[str]):
    """无图时为纯文本，有图时为 OpenAI 多模态 content 数组。"""
    if not images:
        return text
    return [{"type": "text", "text": text}] + [
        {"type": "image_url", "image_url": {"url": u}} for u in images
    ]

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or msg.from_user is None or msg.from_user.is_bot:
        return

    if ENABLE_VISION:
        # 被动记录相册成员（在任何提前 return 之前），供之后回复相册时取整组图片
        remember_album(msg)

    bot = context.bot
    is_private = msg.chat.type == "private"
    replied = msg.reply_to_message
    is_reply_to_bot = bool(replied and replied.from_user and replied.from_user.id == bot.id)
    mentioned = is_mentioned(msg, bot.username, bot.id)

    if not (is_private or mentioned or is_reply_to_bot):
        return

    if not is_authorized(msg.from_user.id):
        logger.info("静默忽略未授权用户 %s (id=%s)", msg.from_user.full_name, msg.from_user.id)
        return

    question = extract_question(msg, bot.username)
    logger.info(
        "收到请求 chat=%s(%s) user=%s(%s) reply_to_bot=%s q=%.80s",
        msg.chat_id, msg.chat.type, msg.from_user.full_name, msg.from_user.id,
        is_reply_to_bot, question,
    )

    if is_reply_to_bot and not mentioned:
        # 追问：接上之前的对话历史
        key = (msg.chat_id, replied.message_id)
        history = conversations.get(key)
        if history is None:
            # 历史已过期（如 bot 重启），用 bot 上一条回复作为最小上下文
            history = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "assistant", "content": replied.text or replied.caption or ""},
            ]
        images = await image_data_urls(bot, msg) if ENABLE_VISION else []
        if not question and not images:
            return
        history = history + [{"role": "user", "content": with_time(build_content(question or t("look_image"), images))}]
    else:
        # 新对话：@提及（群聊）或私聊直接提问
        quoted = None if is_reply_to_bot else replied
        context_text = quoted_context(msg) if quoted else None
        images = await image_data_urls(bot, msg, quoted) if ENABLE_VISION else []
        if not question and not context_text and not images:
            await msg.reply_text(t("nudge"))
            return
        user_content = question or t("comment_default")
        if context_text:
            user_content = context_text + "\n\n" + t("question_from", name=msg.from_user.full_name, question=user_content)
        history = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": with_time(build_content(user_content, images))},
        ]

    history = trim_history(history)

    sent, answer = await stream_reply(msg, history)
    if sent is not None and answer:
        logger.info("已回复 chat=%s msg_id=%s len=%d", msg.chat_id, sent.message_id, len(answer))
        remember(msg.chat_id, sent.message_id, history + [{"role": "assistant", "content": answer}])
    else:
        logger.warning("未产生回复 chat=%s user=%s", msg.chat_id, msg.from_user.id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not is_authorized(user.id):
        return
    await update.effective_message.reply_text(
        t("start", username=context.bot.username, user_id=user.id)
    )


def _target_user_ids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[set[int], str | None]:
    """解析管理命令的目标用户：优先取命令参数里的 ID，否则取被回复消息的发送者。"""
    ids = set()
    for arg in context.args or []:
        try:
            ids.add(int(arg.strip().rstrip(",，")))
        except ValueError:
            return set(), t("invalid_id", arg=arg)
    if not ids:
        replied = update.effective_message.reply_to_message
        if replied and replied.from_user:
            ids.add(replied.from_user.id)
    if not ids:
        return set(), t("admin_usage")
    return ids, None


async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not is_admin(user.id):
        return
    ids, err = _target_user_ids(update, context)
    if err:
        await update.effective_message.reply_text(err)
        return
    allowed_users.update(ids)
    save_allowed_users()
    await update.effective_message.reply_text(
        t("added", ids=", ".join(map(str, sorted(ids))), n=len(allowed_users))
    )


async def cmd_deluser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not is_admin(user.id):
        return
    ids, err = _target_user_ids(update, context)
    if err:
        await update.effective_message.reply_text(err)
        return
    removed = ids & allowed_users
    allowed_users.difference_update(ids)
    save_allowed_users()
    await update.effective_message.reply_text(
        t("removed",
          ids=", ".join(map(str, sorted(removed))) if removed else t("no_match"),
          n=len(allowed_users))
    )


async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not is_admin(user.id):
        return
    lines = [t("admins", ids=", ".join(map(str, sorted(ADMIN_USER_IDS))) or t("not_configured"))]
    if allowed_users:
        lines.append(t("whitelist", n=len(allowed_users), ids="\n".join(map(str, sorted(allowed_users)))))
    else:
        lines.append(t("whitelist_empty_controlled") if ADMIN_USER_IDS else t("whitelist_empty_open"))
    await update.effective_message.reply_text("\n".join(lines))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("处理更新时发生未捕获异常", exc_info=context.error)


async def post_init(app: Application) -> None:
    """启动时向 Telegram 注册命令菜单：所有人可见基础命令，管理员私聊可见管理命令。"""
    from telegram import BotCommandScopeChat

    base = [BotCommand("help", t("cmd_help"))]
    admin_cmds = base + [
        BotCommand("adduser", t("cmd_adduser")),
        BotCommand("deluser", t("cmd_deluser")),
        BotCommand("listusers", t("cmd_listusers")),
    ]
    await app.bot.set_my_commands(base)
    for admin_id in ADMIN_USER_IDS:
        try:
            await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramError as e:
            # 管理员还没和 bot 私聊过时会 chat not found，对方先发个 /start 后重启即可
            logger.warning("为管理员 %s 注册命令菜单失败：%s", admin_id, e)


def build_application() -> Application:
    return (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )


def main() -> None:
    app = build_application()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("deluser", cmd_deluser))
    app.add_handler(CommandHandler("listusers", cmd_listusers))
    app.add_handler(CallbackQueryHandler(on_cancel_button, pattern=r"^c:\d+$"))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
            handle_message,
        )
    )
    logger.info("Bot 启动中… 模型接口: %s, 模型: %s", LLM_BASE_URL, LLM_MODEL)
    if config.GEMINI_NATIVE_SEARCH:
        logger.info("Gemini 原生搜索模式：google_search + url_context 由 Google 服务端执行")
    elif config.GEMINI_SEARCH_MODEL:
        logger.info(
            "混合搜索模式：web_search 由 %s + google_search grounding 执行（失败回退自带搜索源）",
            config.GEMINI_SEARCH_MODEL,
        )
    if config.SEARCH_ENABLED:
        logger.info(
            "联网搜索已开启：provider=%s（web_search + open_url）", ",".join(config.ACTIVE_PROVIDERS)
        )
    skipped = [p for p in config.SEARCH_PROVIDERS if p not in config.ACTIVE_PROVIDERS]
    if skipped:
        logger.warning(
            "搜索源 %s 配置不完整或名称不识别（tavily 需 TAVILY_API_KEY，searxng 需 SEARXNG_BASE_URL），已跳过",
            ",".join(skipped),
        )
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
