"""
群聊 AI 助手 Bot —— 类似 X 上的 @grok 用法：
  - 在群里回复某条消息并 @bot 提问（如 "@bot 这是真的吗？"），bot 会结合被回复的消息内容回答
  - 直接 @bot 提问
  - 回复 bot 的消息可以继续追问，形成多轮对话
  - 私聊中直接发消息即可

后端为任意 OpenAI 兼容接口（LM Studio / vLLM / llama.cpp server 等）。
"""

import asyncio
import base64
import json
import logging
import os
from collections import OrderedDict
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI, BadRequestError
from telegram import BotCommand, Message, Update
from telegram.constants import ChatAction, MessageEntityType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:1234/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "local-model")
LLM_API_KEY = os.getenv("LLM_API_KEY", "not-needed")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1024"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))
# 模型支持图片理解（多模态）时设为 true：群友发图或回复图片提问，图片会一并发给模型
ENABLE_VISION = os.getenv("ENABLE_VISION", "false").strip().lower() in ("1", "true", "yes", "on")
MAX_IMAGES = 4  # 单次请求最多附带的图片数
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# 逗号分隔的超级管理员用户 ID，可随时用 /adduser /deluser 管理白名单
ADMIN_USER_IDS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace("，", ",").split(",") if x.strip()}
# 逗号分隔的用户 ID 白名单（仅作为首次启动的初始值，之后以 allowed_users.json 为准）
ALLOWED_USER_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace("，", ",").split(",") if x.strip()}

WHITELIST_FILE = Path(os.getenv("WHITELIST_FILE", str(Path(__file__).with_name("allowed_users.json"))))
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "你是一个 Telegram 群聊里的 AI 助手。群友会引用一条消息并向你提问"
    "（例如「这是真的吗？」），请结合被引用的内容直接、简洁地回答。"
    "用提问者使用的语言回复。不确定的事情要明确说明，不要编造。",
)

TG_MESSAGE_LIMIT = 4096
CONVERSATION_CACHE_SIZE = 500

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

llm = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)


def load_allowed_users() -> set[int]:
    """白名单：优先读 allowed_users.json（运行时增删的结果），首次启动用 .env 初始值。"""
    if WHITELIST_FILE.exists():
        try:
            return {int(x) for x in json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))}
        except (ValueError, json.JSONDecodeError):
            logger.warning("allowed_users.json 解析失败，回退到 .env 中的 ALLOWED_USER_IDS")
    return set(ALLOWED_USER_IDS)


def save_allowed_users() -> None:
    WHITELIST_FILE.write_text(json.dumps(sorted(allowed_users)), encoding="utf-8")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def is_authorized(user_id: int) -> bool:
    """管理员永远可用；配置了管理员或白名单后即进入受控模式，否则对所有人开放。"""
    if is_admin(user_id):
        return True
    if not ADMIN_USER_IDS and not allowed_users:
        return True
    return user_id in allowed_users

# 对话历史：key = (chat_id, bot 回复消息的 message_id)，value = OpenAI 格式的 messages 列表。
# 用户回复 bot 的某条消息时，就能接上那条消息对应的上下文继续聊。
conversations: "OrderedDict[tuple[int, int], list[dict]]" = OrderedDict()

allowed_users: set[int] = load_allowed_users()


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
    author = replied.from_user.full_name if replied.from_user else "某人"
    return f"以下是群里 {author} 发的一条消息：\n「{content}」"


async def keep_typing(bot, chat_id: int, stop: asyncio.Event) -> None:
    """LLM 生成期间持续显示「正在输入…」。"""
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.5)
        except asyncio.TimeoutError:
            continue


async def image_data_urls(bot, *messages: Message | None) -> list[str]:
    """提取消息中的图片（压缩照片或图片文件），转为 base64 data URL。"""
    urls = []
    for m in messages:
        if m is None:
            continue
        file_id, mime = None, "image/jpeg"
        if m.photo:
            file_id = m.photo[-1].file_id  # 最大尺寸的一张
        elif m.document and (m.document.mime_type or "").startswith("image/"):
            if m.document.file_size and m.document.file_size > MAX_IMAGE_BYTES:
                continue
            file_id, mime = m.document.file_id, m.document.mime_type
        if file_id is None:
            continue
        try:
            file = await bot.get_file(file_id)
            data = bytes(await file.download_as_bytearray())
        except Exception:
            logger.exception("下载图片失败 file_id=%s", file_id)
            continue
        urls.append(f"data:{mime};base64," + base64.b64encode(data).decode())
        if len(urls) >= MAX_IMAGES:
            break
    return urls


def build_content(text: str, images: list[str]):
    """无图时为纯文本，有图时为 OpenAI 多模态 content 数组。"""
    if not images:
        return text
    return [{"type": "text", "text": text}] + [
        {"type": "image_url", "image_url": {"url": u}} for u in images
    ]


async def ask_llm(history: list[dict]) -> str:
    try:
        response = await llm.chat.completions.create(
            model=LLM_MODEL,
            messages=history,
            max_tokens=MAX_TOKENS,
        )
    except BadRequestError as e:
        # OpenAI 官方较新的模型要求用 max_completion_tokens 代替 max_tokens
        if "max_completion_tokens" not in str(e):
            raise
        response = await llm.chat.completions.create(
            model=LLM_MODEL,
            messages=history,
            max_completion_tokens=MAX_TOKENS,
        )
    return (response.choices[0].message.content or "").strip()


async def send_reply(msg: Message, text: str) -> Message:
    """回复消息，处理超长拆分与 Markdown 解析失败的回退。返回最后一条已发送消息。"""
    chunks = [text[i : i + TG_MESSAGE_LIMIT] for i in range(0, len(text), TG_MESSAGE_LIMIT)] or ["（模型返回了空回复）"]
    sent = None
    for chunk in chunks:
        try:
            sent = await msg.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            sent = await msg.reply_text(chunk)
    return sent


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or msg.from_user is None or msg.from_user.is_bot:
        return

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
        history = history + [{"role": "user", "content": build_content(question or "请看这张图片。", images)}]
    else:
        # 新对话：@提及（群聊）或私聊直接提问
        quoted = None if is_reply_to_bot else replied
        context_text = quoted_context(msg) if quoted else None
        images = await image_data_urls(bot, msg, quoted) if ENABLE_VISION else []
        if not question and not context_text and not images:
            await msg.reply_text("请在 @ 我的同时提出问题，或回复某条消息后 @ 我提问～")
            return
        user_content = question or "请评论/核实这条消息。"
        if context_text:
            user_content = f"{context_text}\n\n{msg.from_user.full_name} 的提问：{user_content}"
        history = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_content(user_content, images)},
        ]

    history = trim_history(history)

    stop = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(bot, msg.chat_id, stop))
    try:
        answer = await ask_llm(history)
    except Exception:
        logger.exception("调用本地 LLM 失败")
        stop.set()
        await typing_task
        await msg.reply_text(f"⚠️ 调用本地模型失败，请检查 {LLM_BASE_URL} 服务是否在运行。")
        return
    finally:
        stop.set()
    await typing_task

    sent = await send_reply(msg, answer or "（模型返回了空回复）")
    if sent:
        remember(msg.chat_id, sent.message_id, history + [{"role": "assistant", "content": answer}])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not is_authorized(user.id):
        return
    await update.effective_message.reply_text(
        "你好！把我拉进群后这样用：\n"
        "1️⃣ 回复某条消息并 @ 我提问，例如「@{username} 这是真的吗？」\n"
        "2️⃣ 直接 @ 我提问任何问题\n"
        "3️⃣ 回复我的消息可以继续追问\n"
        "私聊里直接发消息即可。\n\n"
        "你的用户 ID：{user_id}".format(username=context.bot.username, user_id=user.id)
    )


def _target_user_ids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[set[int], str | None]:
    """解析管理命令的目标用户：优先取命令参数里的 ID，否则取被回复消息的发送者。"""
    ids = set()
    for arg in context.args or []:
        try:
            ids.add(int(arg.strip().rstrip(",，")))
        except ValueError:
            return set(), f"「{arg}」不是有效的用户 ID"
    if not ids:
        replied = update.effective_message.reply_to_message
        if replied and replied.from_user:
            ids.add(replied.from_user.id)
    if not ids:
        return set(), "用法：/adduser <用户ID>（可多个，空格分隔），或在群里回复某人的消息后发送该命令"
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
        "✅ 已添加：{ids}\n当前白名单共 {n} 人".format(ids=", ".join(map(str, sorted(ids))), n=len(allowed_users))
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
        "✅ 已移除：{ids}\n当前白名单共 {n} 人".format(
            ids=", ".join(map(str, sorted(removed))) if removed else "（无匹配，名单未变化）",
            n=len(allowed_users),
        )
    )


async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not is_admin(user.id):
        return
    lines = [f"管理员：{', '.join(map(str, sorted(ADMIN_USER_IDS))) or '（未配置）'}"]
    if allowed_users:
        lines.append("白名单（{n} 人）：\n{ids}".format(n=len(allowed_users), ids="\n".join(map(str, sorted(allowed_users)))))
    else:
        lines.append("白名单为空" + ("（受控模式：仅管理员可用）" if ADMIN_USER_IDS else "（开放模式：所有人可用）"))
    await update.effective_message.reply_text("\n".join(lines))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("处理更新时发生未捕获异常", exc_info=context.error)


async def post_init(app: Application) -> None:
    """启动时向 Telegram 注册命令菜单：所有人可见基础命令，管理员私聊可见管理命令。"""
    from telegram import BotCommandScopeChat

    base = [BotCommand("help", "使用说明")]
    admin_cmds = base + [
        BotCommand("adduser", "添加白名单用户（ID 或回复某人消息）"),
        BotCommand("deluser", "移除白名单用户"),
        BotCommand("listusers", "查看白名单"),
    ]
    await app.bot.set_my_commands(base)
    for admin_id in ADMIN_USER_IDS:
        try:
            await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramError as e:
            # 管理员还没和 bot 私聊过时会 chat not found，对方先发个 /start 后重启即可
            logger.warning("为管理员 %s 注册命令菜单失败：%s", admin_id, e)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("deluser", cmd_deluser))
    app.add_handler(CommandHandler("listusers", cmd_listusers))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
            handle_message,
        )
    )
    logger.info("Bot 启动中… 模型接口: %s, 模型: %s", LLM_BASE_URL, LLM_MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
