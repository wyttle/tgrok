"""Telegram 接入层：消息路由、图片/相册、管理命令与应用入口。"""

import asyncio
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
    CONVERSATION_CONTENT_BUDGET, ENABLE_VISION, LLM_BASE_URL, LLM_MODEL,
    MAX_HISTORY, MAX_IMAGE_BYTES,
)
from .chat import on_cancel_button, stream_reply
from .i18n import t
from .prompt import SYSTEM_PROMPT, with_time
from .tg_auth import allowed_users, is_admin, is_authorized, save_allowed_users

logger = logging.getLogger(__name__)

# 对话历史：key = (chat_id, bot 回复消息的 message_id)，value = OpenAI 格式的 messages 列表。
# 用户回复 bot 的某条消息时，就能接上那条消息对应的上下文继续聊。
conversations: "OrderedDict[tuple[int, int], list[dict]]" = OrderedDict()
_conv_sizes: dict[tuple[int, int], int] = {}
_conv_total = 0


def _history_chars(history: list[dict]) -> int:
    """近似内容量：文本取字符长度，多模态取各块文本/data URL 长度之和。"""
    total = 0
    for message in history:
        content = message.get("content", "")
        if isinstance(content, str):
            total += len(content)
        else:
            for part in content:
                total += len(part.get("text", ""))
                total += len((part.get("image_url") or {}).get("url", ""))
    return total


def remember(chat_id: int, message_id: int, history: list[dict]) -> None:
    global _conv_total
    key = (chat_id, message_id)
    _conv_total -= _conv_sizes.get(key, 0)
    size = _history_chars(history)
    conversations[key] = history
    _conv_sizes[key] = size
    _conv_total += size
    conversations.move_to_end(key)
    while len(conversations) > 1 and (
        len(conversations) > CONVERSATION_CACHE_SIZE
        or _conv_total > CONVERSATION_CONTENT_BUDGET
    ):
        oldest, _ = conversations.popitem(last=False)
        _conv_total -= _conv_sizes.pop(oldest)


def trim_history(history: list[dict]) -> list[dict]:
    """保留 system 消息 + 最近 MAX_HISTORY 条对话，且截断后第一条对话必须是 user。

    以 assistant 开头的历史会被 Claude 原生接口和多数本地模型的对话模板
    （要求 user/assistant 严格交替）拒绝，也会丢掉那条回答对应的问题。
    """
    if len(history) <= MAX_HISTORY + 1:
        return history
    tail = history[-MAX_HISTORY:]
    start = next((i for i, m in enumerate(tail) if m["role"] == "user"), len(tail))
    return [history[0]] + tail[start:]

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

    async def fetch(entry: dict) -> str | None:
        try:
            file = await bot.get_file(entry["file_id"])
            data = bytes(await file.download_as_bytearray())
        except Exception:
            logger.exception("下载图片失败 file_id=%s", entry["file_id"])
            return None
        return f"data:{entry['mime']};base64," + base64.b64encode(data).decode()

    # 相册多图并发下载：每张图都是 get_file + 下载两次往返，串行时首字延迟随图片数线性增长
    urls = await asyncio.gather(*(fetch(entry) for entry in refs[:config.MAX_IMAGES]))
    return [u for u in urls if u]


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

    # 群里多人可以轮流接着同一段对话聊：每条 user 消息都带上说话人，模型才分得清谁在问
    def speaker(text: str) -> str:
        return text if is_private else t("question_from", name=msg.from_user.full_name, question=text)

    if is_reply_to_bot:
        # 追问（回复 bot 的消息，带不带 @ 都算）：接上之前的对话历史
        images = await image_data_urls(bot, msg) if ENABLE_VISION else []
        if not question and not images:
            return
        user_content = speaker(question or t("look_image"))
        history = conversations.get((msg.chat_id, replied.message_id))
        if history is None:
            # 历史已过期（如 bot 重启）：把 bot 那条回复并入本轮 user 消息作为最小上下文，
            # 不单独伪造 assistant 轮——以 assistant 开头的对话会被严格交替的接口拒绝
            previous = replied.text or replied.caption or ""
            if previous:
                user_content = t("prev_reply", content=previous) + "\n\n" + user_content
            history = [{"role": "system", "content": SYSTEM_PROMPT}]
        history = history + [{"role": "user", "content": with_time(build_content(user_content, images))}]
    else:
        # 新对话：@提及（群聊）或私聊直接提问
        context_text = quoted_context(msg) if replied else None
        images = await image_data_urls(bot, msg, replied) if ENABLE_VISION else []
        if not question and not context_text and not images:
            await msg.reply_text(t("nudge"))
            return
        user_content = question or t("comment_default")
        if context_text:
            user_content = context_text + "\n\n" + t("question_from", name=msg.from_user.full_name, question=user_content)
        else:
            user_content = speaker(user_content)
        history = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": with_time(build_content(user_content, images))},
        ]

    history = trim_history(history)

    answer_ids, answer = await stream_reply(msg, history)
    if answer_ids and answer:
        logger.info("已回复 chat=%s msg_ids=%s len=%d", msg.chat_id, answer_ids, len(answer))
        full = history + [{"role": "assistant", "content": answer}]
        for message_id in answer_ids:
            remember(msg.chat_id, message_id, full)
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
    if config.CLAUDE_NATIVE:
        api_url = config.CLAUDE_BASE_URL or "https://api.anthropic.com"
    elif config.GEMINI_NATIVE_SEARCH:
        api_url = config.GEMINI_BASE_URL or "https://generativelanguage.googleapis.com"
    else:
        api_url = LLM_BASE_URL
    logger.info(
        "Bot 启动中… 协议: %s, 接口: %s, 模型: %s", config.LLM_PROTOCOL, api_url, LLM_MODEL
    )
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
    # 只订阅用到的更新类型：不收 edited_message，群友编辑一条 @bot 的旧消息不会触发重复回答
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY], drop_pending_updates=True)


if __name__ == "__main__":
    main()
