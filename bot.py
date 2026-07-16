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
import html
import ipaddress
import json
import logging
import os
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI, BadRequestError
import telegramify_markdown
from telegram import BotCommand, Message, Update
from telegram.constants import MessageEntityType, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
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
# 自定义请求的 User-Agent（部分云端网关会校验 UA），留空使用 SDK 默认值
LLM_USER_AGENT = os.getenv("LLM_USER_AGENT", "").strip()
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1024"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))
# 模型支持图片理解（多模态）时设为 true：群友发图或回复图片提问，图片会一并发给模型
ENABLE_VISION = os.getenv("ENABLE_VISION", "false").strip().lower() in ("1", "true", "yes", "on")
MAX_IMAGES = 4  # 单次请求最多附带的图片数
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# 联网搜索源：tavily / duckduckgo / searxng，留空关闭。可逗号分隔配置多个源，
# 并发聚合结果（如 SEARCH_PROVIDER=tavily,duckduckgo）。开启后模型可通过
# web_search 工具自主搜索，并可用 open_url 工具读取网页正文。
SEARCH_PROVIDERS = [
    p.strip() for p in os.getenv("SEARCH_PROVIDER", "").replace("，", ",").lower().split(",") if p.strip()
]
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
SEARXNG_BASE_URL = os.getenv("SEARXNG_BASE_URL", "").strip().rstrip("/")
SEARCH_MAX_RESULTS = int(os.getenv("SEARCH_MAX_RESULTS", "5"))
SEARCH_MAX_ROUNDS = int(os.getenv("SEARCH_MAX_ROUNDS", "3"))  # 单次回答最多执行工具调用的轮数
SEARCH_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "12"))
SEARCH_RESULT_CHAR_LIMIT = 2400  # 单次回灌给模型的搜索结果文本上限（保护小模型上下文）
SEARCH_SNIPPET_LIMIT = 400  # 单条结果摘要的长度上限
FETCH_CHAR_LIMIT = int(os.getenv("FETCH_CHAR_LIMIT", "3500"))  # 单次回灌给模型的网页正文上限
# open_url 直接抓取失败（反爬 403 / JS 页面 / 正文过少）时，自动改走 Jina Reader 再试
JINA_FALLBACK = os.getenv("JINA_FALLBACK", "true").strip().lower() in ("1", "true", "yes", "on")
JINA_API_KEY = os.getenv("JINA_API_KEY", "").strip()  # 可选，配置后速率限制更宽松


def _provider_ready(p: str) -> bool:
    if p == "tavily":
        return bool(TAVILY_API_KEY)
    if p == "searxng":
        return bool(SEARXNG_BASE_URL)
    return p == "duckduckgo"


ACTIVE_PROVIDERS = [p for p in SEARCH_PROVIDERS if _provider_ready(p)]
SEARCH_ENABLED = bool(ACTIVE_PROVIDERS)
# 逗号分隔的超级管理员用户 ID，可随时用 /adduser /deluser 管理白名单
ADMIN_USER_IDS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace("，", ",").split(",") if x.strip()}
# 逗号分隔的用户 ID 白名单（仅作为首次启动的初始值，之后以 allowed_users.json 为准）
ALLOWED_USER_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace("，", ",").split(",") if x.strip()}

WHITELIST_FILE = Path(os.getenv("WHITELIST_FILE", str(Path(__file__).with_name("allowed_users.json"))))
BOT_LANG = os.getenv("BOT_LANG", "zh").strip().lower()
if BOT_LANG not in ("zh", "en"):
    BOT_LANG = "zh"

# 时区：用于在每次请求时告诉模型"现在的真实时间"，避免它瞎猜日期或谎称已核实
BOT_TZ_NAME = os.getenv("BOT_TZ", "Asia/Shanghai").strip() or "Asia/Shanghai"
try:
    BOT_TZ = ZoneInfo(BOT_TZ_NAME)
except (ZoneInfoNotFoundError, ValueError):
    # 回退用标准库的 timezone.utc，它不依赖系统/tzdata，任何环境都可用
    # （ZoneInfo("UTC") 在缺 tzdata 时同样会抛异常，不能用作兜底）
    logging.getLogger(__name__).warning("无法识别时区 %s，回退到 UTC", BOT_TZ_NAME)
    BOT_TZ_NAME, BOT_TZ = "UTC", timezone.utc

STRINGS = {
    "zh": {
        "system_prompt": (
            "你是一个 Telegram 群聊里的 AI 助手。群友会引用一条消息并向你提问"
            "（例如「这是真的吗？」），请结合被引用的内容直接、简洁地回答。"
            "用提问者使用的语言回复。不确定的事情要明确说明，不要编造。"
        ),
        "someone": "某人",
        "quoted_msg": "以下是群里 {author} 发的一条消息：\n「{content}」",
        "question_from": "{name} 的提问：{question}",
        "comment_default": "请评论/核实这条消息。",
        "look_image": "请看这张图片。",
        "empty_reply": "（模型返回了空回复）",
        "thinking": "🤔 Thinking…",
        "nudge": "请在 @ 我的同时提出问题，或回复某条消息后 @ 我提问～",
        "llm_failed": "⚠️ 调用模型失败，请检查 {url} 服务是否可用。",
        "searching": "🔍 正在搜索：{query}…",
        "search_more": "🔍 正在搜索 {n} 项…",
        "search_no_results": "（没有找到「{query}」的联网搜索结果）",
        "search_error": "（联网搜索失败：{error}。请基于已有知识回答，并说明信息未经联网核实。）",
        "search_bad_args": "（工具调用参数无法解析，请用合法的 JSON 参数重新调用工具）",
        "opening": "🌐 正在读取网页：{url}…",
        "fetch_bad_url": "（无法读取该地址：仅支持公网 http/https 链接）",
        "fetch_error": "（读取网页失败：{error}。可换一条链接重试，或基于搜索摘要回答。）",
        "fetch_unsupported": "（该链接不是文本网页（{ctype}），无法读取）",
        "fetch_empty": "（该网页没有可提取的正文）",
        "search_system_prompt": (
            "你可以调用 web_search 工具联网搜索实时信息，也可以调用 open_url 工具"
            "读取网页正文（例如搜索结果里的链接）获取细节。"
            "遇到时事、时效性内容或不确定的事实时，先搜索、必要时打开网页核实再回答，"
            "并在答案中附上来源链接。"
        ),
        "current_time": (
            "当前真实时间是 {time}（{tz}），这是系统提供的准确时间，可直接引用。"
            "涉及「今天/现在/最近」等时间时以此为准，不要臆测日期，也不要谎称已核实。"
        ),
        "weekday": ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"],
        "start": (
            "你好！把我拉进群后这样用：\n"
            "1️⃣ 回复某条消息并 @ 我提问，例如「@{username} 这是真的吗？」\n"
            "2️⃣ 直接 @ 我提问任何问题\n"
            "3️⃣ 回复我的消息可以继续追问\n"
            "私聊里直接发消息即可。\n\n"
            "你的用户 ID：{user_id}"
        ),
        "admin_usage": "用法：/adduser <用户ID>（可多个，空格分隔），或在群里回复某人的消息后发送该命令",
        "invalid_id": "「{arg}」不是有效的用户 ID",
        "added": "✅ 已添加：{ids}\n当前白名单共 {n} 人",
        "removed": "✅ 已移除：{ids}\n当前白名单共 {n} 人",
        "no_match": "（无匹配，名单未变化）",
        "admins": "管理员：{ids}",
        "not_configured": "（未配置）",
        "whitelist": "白名单（{n} 人）：\n{ids}",
        "whitelist_empty_controlled": "白名单为空（受控模式：仅管理员可用）",
        "whitelist_empty_open": "白名单为空（开放模式：所有人可用）",
        "cmd_help": "使用说明",
        "cmd_adduser": "添加白名单用户（ID 或回复某人消息）",
        "cmd_deluser": "移除白名单用户",
        "cmd_listusers": "查看白名单",
    },
    "en": {
        "system_prompt": (
            "You are an AI assistant in a Telegram group chat. Members will quote a message "
            "and ask you about it (e.g. \"is this true?\"). Answer directly and concisely "
            "based on the quoted content. Reply in the language the asker uses. "
            "Be explicit about uncertainty and never make things up."
        ),
        "someone": "someone",
        "quoted_msg": "Here is a message {author} sent in the group:\n\"{content}\"",
        "question_from": "{name} asks: {question}",
        "comment_default": "Please comment on / fact-check this message.",
        "look_image": "Please look at this image.",
        "empty_reply": "(the model returned an empty response)",
        "thinking": "🤔 Thinking…",
        "nudge": "Please include a question when mentioning me, or reply to a message and mention me.",
        "llm_failed": "⚠️ Failed to call the model. Please check that {url} is reachable.",
        "searching": "🔍 Searching: {query}…",
        "search_more": "🔍 Running {n} searches…",
        "search_no_results": "(no web search results found for \"{query}\")",
        "search_error": "(web search failed: {error}. Answer from your own knowledge and note it was not verified online.)",
        "search_bad_args": "(could not parse the tool arguments; call the tool again with valid JSON arguments)",
        "opening": "🌐 Reading page: {url}…",
        "fetch_bad_url": "(cannot fetch this address: only public http/https URLs are supported)",
        "fetch_error": "(failed to fetch the page: {error}. Try another link or answer from the search snippets.)",
        "fetch_unsupported": "(the link is not a text page ({ctype}), cannot read it)",
        "fetch_empty": "(no readable text on that page)",
        "search_system_prompt": (
            "You can call the web_search tool to look up real-time information on the internet, "
            "and the open_url tool to read the text of a web page (e.g. a link from search results) "
            "for details. For current events, time-sensitive topics, or facts you are unsure about, "
            "search first, open pages to verify when needed, then answer and cite source links."
        ),
        "current_time": (
            "The current real-world time is {time} ({tz}). This is accurate time provided by the "
            "system and can be cited directly. Use it for anything involving \"today/now/recently\"; "
            "do not guess the date or claim you have verified it."
        ),
        "weekday": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "start": (
            "Hi! Add me to a group and use me like this:\n"
            "1️⃣ Reply to any message and mention me with a question, e.g. \"@{username} is this true?\"\n"
            "2️⃣ Mention me directly with any question\n"
            "3️⃣ Reply to my messages to follow up\n"
            "In private chat, just send a message.\n\n"
            "Your user ID: {user_id}"
        ),
        "admin_usage": "Usage: /adduser <user ID> (multiple IDs separated by spaces), or reply to someone's message with this command",
        "invalid_id": "\"{arg}\" is not a valid user ID",
        "added": "✅ Added: {ids}\nWhitelist now has {n} user(s)",
        "removed": "✅ Removed: {ids}\nWhitelist now has {n} user(s)",
        "no_match": "(no match, list unchanged)",
        "admins": "Admins: {ids}",
        "not_configured": "(not configured)",
        "whitelist": "Whitelist ({n} user(s)):\n{ids}",
        "whitelist_empty_controlled": "Whitelist is empty (controlled mode: admins only)",
        "whitelist_empty_open": "Whitelist is empty (open mode: everyone can use)",
        "cmd_help": "How to use",
        "cmd_adduser": "Add user to whitelist (ID or reply to a message)",
        "cmd_deluser": "Remove user from whitelist",
        "cmd_listusers": "Show whitelist",
    },
}


def t(key: str, **kwargs) -> str:
    return STRINGS[BOT_LANG][key].format(**kwargs)


SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", t("system_prompt"))
if SEARCH_ENABLED:
    # 明确告知模型它拥有联网搜索能力，避免它声称"我无法联网"
    SYSTEM_PROMPT += "\n\n" + t("search_system_prompt")


def current_time_line() -> str:
    """返回一句描述当前真实时间的文本，随每次请求实时生成。

    附加到「当前这条用户消息」末尾，而非系统提示：系统提示与历史轮次保持字节
    不变，才能命中上游的 prompt 缓存；时间只挂在本来就是新内容的最新一轮上。
    """
    now = datetime.now(BOT_TZ)
    weekday = STRINGS[BOT_LANG]["weekday"][now.weekday()]
    stamp = f"{now:%Y-%m-%d %H:%M} {weekday}"
    return t("current_time", time=stamp, tz=BOT_TZ_NAME)


def with_time(content):
    """把当前时间行拼到用户消息内容末尾，兼容纯文本与多模态 content 数组。"""
    line = current_time_line()
    if isinstance(content, str):
        return f"{content}\n\n[{line}]"
    # 多模态：追加到文本块（首个 text 块），没有则插一个
    for part in content:
        if part.get("type") == "text":
            part["text"] = f"{part['text']}\n\n[{line}]"
            return content
    return [{"type": "text", "text": f"[{line}]"}] + content

TG_MESSAGE_LIMIT = 4096
CONVERSATION_CACHE_SIZE = 500
STREAM_EDIT_INTERVAL = 1.5  # 流式输出时编辑消息的最小间隔（秒），避免触发 Telegram 限流
STREAM_SEGMENT_LIMIT = 3800  # 单条消息承载的流式文本上限，超过则另起一条（留出余量）
STREAM_CURSOR = " ▌"

# 匹配标题行：行首可选的 emoji/符号前缀 + 1~6 个 #。Grok 常输出「📚 ## 标题」这种
# 前缀带 emoji 的标题，此时 # 不在行首，telegramify/CommonMark 不认作标题，会把 ## 原样
# 泄漏成 \#\#。这里把前缀 emoji 去掉、# 归位到行首，让下游正常渲染成加粗。
_HEADING_RE = re.compile(r"^[ \t]*[^\w#\n]*[ \t]*(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)


def _normalize_headings(text: str) -> str:
    def repl(m: re.Match) -> str:
        return f"{m.group(1)} {m.group(2)}"
    return _HEADING_RE.sub(repl, text)


def to_telegram_markdown(text: str) -> str:
    """把模型返回的标准 Markdown 转成 Telegram MarkdownV2。

    先归一化标题行（去掉 Grok 爱加的 emoji 前缀，让 # 回到行首），再交给
    telegramify_markdown 转换。
    """
    return telegramify_markdown.markdownify(_normalize_headings(text))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

llm = AsyncOpenAI(
    base_url=LLM_BASE_URL,
    api_key=LLM_API_KEY,
    default_headers={"User-Agent": LLM_USER_AGENT} if LLM_USER_AGENT else None,
)


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
    author = replied.from_user.full_name if replied.from_user else t("someone")
    return t("quoted_msg", author=author, content=content)


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


WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for up-to-date information. Use this for recent events, "
            "time-sensitive facts, or anything you are unsure about."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query, in the language most likely to find good results.",
                }
            },
            "required": ["query"],
        },
    },
}

FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "open_url",
        "description": (
            "Fetch a web page by URL and return its readable text. "
            "Use it to read the details behind links found via web_search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full http(s) URL of the page to read.",
                }
            },
            "required": ["url"],
        },
    },
}

SEARCH_TOOLS = [WEB_SEARCH_TOOL, FETCH_URL_TOOL]

# 后端明确拒绝 tools 参数后置 False，进程内不再携带（bot 退化为普通对话）
tools_supported = True


async def _search_tavily(query: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
            json={"query": query, "max_results": SEARCH_MAX_RESULTS, "search_depth": "basic"},
        )
        resp.raise_for_status()
        data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in data.get("results", [])
    ]


async def _search_searxng(query: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
        resp = await client.get(
            f"{SEARXNG_BASE_URL}/search", params={"q": query, "format": "json"}
        )
        resp.raise_for_status()
        data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in data.get("results", [])[:SEARCH_MAX_RESULTS]
    ]


async def _search_duckduckgo(query: str) -> list[dict]:
    from ddgs import DDGS  # 惰性导入：仅 duckduckgo 源需要安装 ddgs

    def _run() -> list[dict]:
        with DDGS(timeout=SEARCH_TIMEOUT) as ddgs:
            return list(ddgs.text(query, max_results=SEARCH_MAX_RESULTS))

    rows = await asyncio.to_thread(_run)
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("href") or r.get("url", ""),
            "snippet": r.get("body") or r.get("description", ""),
        }
        for r in rows
    ]


def format_search_results(query: str, rows: list[dict]) -> str:
    blocks, total = [], 0
    for i, r in enumerate(rows, 1):
        snippet = (r.get("snippet") or "").strip()[:SEARCH_SNIPPET_LIMIT]
        block = f"[{i}] {r.get('title', '')}\n{r.get('url', '')}\n{snippet}"
        if total + len(block) > SEARCH_RESULT_CHAR_LIMIT:
            break
        blocks.append(block)
        total += len(block)
    if not blocks:
        return t("search_no_results", query=query)
    return "\n\n".join(blocks)


_PROVIDER_SEARCH = {
    "tavily": _search_tavily,
    "searxng": _search_searxng,
    "duckduckgo": _search_duckduckgo,
}


async def run_web_search(query: str) -> str:
    """并发聚合所有已配置的搜索源。永不抛异常：失败返回让模型能继续作答的说明文本。"""
    query = (query or "").strip()
    if not query:
        return t("search_no_results", query="")
    logger.info("联网搜索 (%s): %s", "+".join(ACTIVE_PROVIDERS), query)
    outcomes = await asyncio.gather(
        *(_PROVIDER_SEARCH[p](query) for p in ACTIVE_PROVIDERS), return_exceptions=True
    )
    grouped, first_error = [], None
    for provider, outcome in zip(ACTIVE_PROVIDERS, outcomes):
        if isinstance(outcome, BaseException):
            first_error = first_error or outcome
            logger.warning("搜索源 %s 失败：%s: %s", provider, type(outcome).__name__, outcome)
        elif outcome:
            grouped.append(outcome)
    if not grouped:
        if first_error is not None:
            return t("search_error", error=type(first_error).__name__)
        return t("search_no_results", query=query)
    # 各源结果交错合并（每源轮流出一条）并按 URL 去重，保证每个源都有机会排前
    rows, seen = [], set()
    for tier in range(max(len(g) for g in grouped)):
        for g in grouped:
            if tier < len(g):
                r = g[tier]
                key = (r.get("url") or "").split("#")[0].rstrip("/") or r.get("title", "")
                if key not in seen:
                    seen.add(key)
                    rows.append(r)
    return format_search_results(query, rows)


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_HEAD_RE = re.compile(r"<head\b.*?</head\s*>", re.I | re.S)
_MAIN_RE = re.compile(r"<(main|article)\b.*?</\1\s*>", re.I | re.S)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript|svg)\b.*?</\1\s*>", re.I | re.S)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|br|li|ul|ol|tr|table|h[1-6]|section|article|header|footer|blockquote|pre)\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(page: str) -> tuple[str, str]:
    """极简 HTML 正文提取：去掉 script/style/标签，块级标签转换行。返回 (标题, 正文)。"""
    m = _TITLE_RE.search(page)
    title = html.unescape(m.group(1)).strip() if m else ""
    page = _HEAD_RE.sub(" ", page)
    # 页面声明了 main/article 正文区域时只取该区域，省掉导航/页脚等噪音
    m = _MAIN_RE.search(page)
    if m and len(m.group(0)) > 1000:
        page = m.group(0)
    page = _SCRIPT_STYLE_RE.sub(" ", page)
    page = _HTML_COMMENT_RE.sub(" ", page)
    page = _BLOCK_TAG_RE.sub("\n", page)
    page = _TAG_RE.sub(" ", page)
    page = html.unescape(page)
    lines = (re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in page.split("\n"))
    return title, "\n".join(ln for ln in lines if ln)


def _is_public_http_url(url: str) -> bool:
    """只允许公网 http(s) 地址，拒绝内网/回环地址，避免模型探测内网（SSRF）。"""
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if parts.hostname == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return True  # 是域名而非 IP 字面量；DNS 解析级别的校验不在此处做
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)


def _extract_html_text(page: str) -> tuple[str, str]:
    """HTML → (标题, 正文)。优先 trafilatura（正文识别质量最好），失败回退内置极简提取。"""
    m = _TITLE_RE.search(page)
    title = html.unescape(m.group(1)).strip() if m else ""
    text = ""
    try:
        import trafilatura  # 惰性导入：依赖缺失时仍可用内置提取

        try:
            text = trafilatura.extract(
                page, output_format="markdown", include_links=False, include_tables=True
            ) or ""
        except (TypeError, ValueError):  # 旧版本不支持 markdown 输出
            text = trafilatura.extract(page) or ""
    except ImportError:
        pass
    if not text.strip():
        _, text = _html_to_text(page)
    return title, text.strip()


async def _fetch_local(url: str) -> tuple[str | None, str]:
    """直接抓取网页。返回 (成功文本, 失败说明)；文本为 None 表示这条路走不通。"""
    try:
        async with httpx.AsyncClient(
            timeout=SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; tgrok-bot/1.0)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as e:
        logger.warning("直接读取网页失败 %s: %s", url, type(e).__name__)
        return None, t("fetch_error", error=type(e).__name__)
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype and not (ctype.startswith("text/") or ctype in ("application/json", "application/xhtml+xml")):
        return None, t("fetch_unsupported", ctype=ctype)
    raw = resp.text[:200_000]  # 粗截超大页面，再做正文提取
    is_html = "html" in ctype or "<html" in raw[:2000].lower()
    if is_html:
        title, text = await asyncio.to_thread(_extract_html_text, raw)
    else:
        title, text = "", raw.strip()
    if not text or (is_html and len(text) < 200):
        # 正文过少：多半是 JS 渲染的空壳页，交给 Jina Reader 兜底
        return None, t("fetch_empty")
    header = f"{title}\n{resp.url}" if title else str(resp.url)
    return f"{header}\n\n{text}"[:FETCH_CHAR_LIMIT], ""


_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


async def _fetch_jina(url: str) -> str | None:
    """通过 Jina Reader（r.jina.ai）抓取：其服务端渲染 JS 并输出 LLM 友好的 markdown。"""
    headers = {"X-Return-Format": "markdown", "X-Retain-Images": "none"}
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT * 2, follow_redirects=True) as client:
            resp = await client.get(f"https://r.jina.ai/{url}", headers=headers)
            resp.raise_for_status()
    except Exception as e:
        logger.warning("Jina Reader 读取失败 %s: %s", url, type(e).__name__)
        return None
    # 内联链接压成纯文字，URL 不占正文字数配额（页面来源 URL 已在开头单独给出）
    text = _MD_LINK_RE.sub(r"\1", resp.text).strip()
    return text[:FETCH_CHAR_LIMIT] if text else None


async def run_fetch_url(url: str) -> str:
    """抓取网页正文回灌给模型：本地直取为主，Jina Reader 兜底。永不抛异常。"""
    url = (url or "").strip()
    if not _is_public_http_url(url):
        return t("fetch_bad_url")
    logger.info("读取网页: %s", url)
    text, err = await _fetch_local(url)
    if text is not None:
        return text
    if JINA_FALLBACK:
        logger.info("直取失败，改走 Jina Reader: %s", url)
        jina_text = await _fetch_jina(url)
        if jina_text:
            return jina_text
    return err


async def _drain_stream(stream, on_text) -> tuple[dict[int, dict], str]:
    """消费流式响应：正文片段逐个交给 on_text，tool_call 片段按 index 聚合。

    返回（聚合后的 tool_calls, 本轮完整正文）。
    """
    calls: dict[int, dict] = {}
    content = ""
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if delta.content:
            content += delta.content
            await on_text(delta.content)
        for tc in delta.tool_calls or []:
            slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
            if tc.id:
                slot["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function.arguments:
                    slot["arguments"] += tc.function.arguments
    return calls, content


def _assistant_tool_call_msg(calls: dict[int, dict], content: str) -> dict:
    """把聚合好的 tool_call 片段组装成请求格式的 assistant 消息。"""
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                # 部分本地后端不回 id：合成一个，并在 tool 结果里复用以保持配对
                "id": slot["id"] or f"call_{i}",
                "type": "function",
                "function": {
                    "name": slot["name"] or "web_search",
                    "arguments": slot["arguments"] or "{}",
                },
            }
            for i, slot in sorted(calls.items())
        ],
    }


def _tool_args(call: dict) -> dict | None:
    """解析 tool_call 的参数；arguments 不是合法 JSON 对象时返回 None。"""
    try:
        args = json.loads(call["function"]["arguments"])
    except (json.JSONDecodeError, TypeError):
        return None
    return args if isinstance(args, dict) else None


def _call_status(calls: list[dict]) -> str:
    """生成工具调用期间显示给用户的状态文本。"""
    if len(calls) != 1:
        return t("search_more", n=len(calls))
    args = _tool_args(calls[0]) or {}
    if calls[0]["function"]["name"] == "open_url":
        url = str(args.get("url", ""))
        return t("opening", url=url if len(url) <= 80 else url[:77] + "…")
    return t("searching", query=str(args.get("query", "")))


async def _execute_tool_calls(assistant_msg: dict) -> list[dict]:
    results = []
    for call in assistant_msg["tool_calls"]:
        name = call["function"]["name"]
        args = _tool_args(call)
        if args is None:
            content = t("search_bad_args")
        elif name == "open_url":
            content = await run_fetch_url(str(args.get("url", "")))
        else:
            content = await run_web_search(str(args.get("query", "")))
        results.append(
            {"role": "tool", "tool_call_id": call["id"], "name": name, "content": content}
        )
    return results


async def create_stream(history: list[dict], use_tools: bool):
    global tools_supported
    token_param = "max_tokens"
    include_tools = use_tools and tools_supported
    while True:
        kwargs = {"model": LLM_MODEL, "messages": history, "stream": True, token_param: MAX_TOKENS}
        if include_tools:
            kwargs["tools"] = SEARCH_TOOLS
            kwargs["tool_choice"] = "auto"
        try:
            return await llm.chat.completions.create(**kwargs)
        except BadRequestError as e:
            err = str(e).lower()
            # OpenAI 官方较新的模型要求用 max_completion_tokens 代替 max_tokens
            if token_param == "max_tokens" and "max_completion_tokens" in err:
                token_param = "max_completion_tokens"
                continue
            # 后端不支持 function calling：去掉 tools 重试，并在进程内粘性禁用
            if include_tools and "tool" in err:
                logger.warning("后端拒绝 tools 参数，联网搜索已禁用（重启进程后会再次尝试）：%s", e)
                tools_supported = False
                include_tools = False
                continue
            raise


async def stream_reply(msg: Message, history: list[dict]) -> tuple[Message | None, str]:
    """流式生成并逐步编辑 Telegram 消息，支持模型通过 web_search 工具联网搜索。

    先发送思考占位提示（推理模型思考期间无正文输出），首个正文数据块到达后原地替换；
    单条消息超过 STREAM_SEGMENT_LIMIT 时定稿当前消息、另起一条继续。
    模型请求搜索时在当前消息上显示 🔍 状态，执行后把结果回灌给模型继续生成
    （最多 SEARCH_MAX_ROUNDS 轮）。传入的 history 不会被修改，中间的 tool
    消息只存在于本次调用内部，不会进入对话缓存。
    返回（最后一条已发送消息或 None, 完整回复文本）；失败/空回复时已就地提示，返回 (None, "")。
    """
    try:
        sent: Message | None = await msg.reply_text(t("thinking"))
    except TelegramError:
        logger.exception("发送占位消息失败")
        return None, ""
    finalized = ""  # 已定稿消息承载的文本
    segment = ""  # 当前消息正在累积的文本
    last_edit = 0.0

    async def push(text: str, final: bool) -> None:
        nonlocal sent
        try:
            if sent is None:
                # 首次发送：定稿走 MarkdownV2，中间过程用纯文本+光标
                if final:
                    try:
                        sent = await msg.reply_text(
                            to_telegram_markdown(text), parse_mode=ParseMode.MARKDOWN_V2
                        )
                    except BadRequest:
                        sent = await msg.reply_text(text)
                else:
                    sent = await msg.reply_text(text + STREAM_CURSOR)
            elif final:
                try:
                    await sent.edit_text(
                        to_telegram_markdown(text), parse_mode=ParseMode.MARKDOWN_V2
                    )
                except BadRequest:
                    await sent.edit_text(text)
            else:
                await sent.edit_text(text + STREAM_CURSOR)
        except RetryAfter as e:
            # Telegram 限流：等待后跳过本次中间编辑；定稿编辑重试一次
            await asyncio.sleep(float(e.retry_after) + 0.5)
            if final and sent is not None:
                try:
                    await sent.edit_text(
                        to_telegram_markdown(text), parse_mode=ParseMode.MARKDOWN_V2
                    )
                except BadRequest:
                    try:
                        await sent.edit_text(text)
                    except TelegramError:
                        pass
                except TelegramError:
                    pass
        except BadRequest:
            pass  # 例如 message is not modified

    async def on_text(delta: str) -> None:
        nonlocal segment, sent, last_edit, finalized
        segment += delta
        if len(segment) >= STREAM_SEGMENT_LIMIT:
            # 当前消息已满：定稿并另起一条
            await push(segment, final=True)
            finalized += segment
            segment, sent, last_edit = "", None, 0.0
            return
        now = time.monotonic()
        if now - last_edit >= STREAM_EDIT_INTERVAL:
            await push(segment, final=False)
            last_edit = now

    async def show_status(text: str) -> None:
        # 搜索状态提示：编辑当前气泡，之后的正文 push 会自然覆盖它
        nonlocal sent, last_edit
        try:
            if sent is None:
                sent = await msg.reply_text(text)
            else:
                await sent.edit_text(text)
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
        except TelegramError:
            pass
        last_edit = 0.0  # 让下一次正文编辑立即生效

    working = list(history)  # 工具消息只追加到副本，调用方的 history 保持干净
    try:
        for round_idx in range(SEARCH_MAX_ROUNDS + 1):
            # 最后一轮不带 tools，强制模型输出正文，防止无限连环搜索
            use_tools = SEARCH_ENABLED and round_idx < SEARCH_MAX_ROUNDS
            stream = await create_stream(working, use_tools=use_tools)
            calls, content = await _drain_stream(stream, on_text)
            if not calls or not use_tools:
                break
            assistant_msg = _assistant_tool_call_msg(calls, content)
            status = _call_status(assistant_msg["tool_calls"])
            if segment.strip():
                # 模型在搜索前已输出部分正文：状态提示追加在正文之后显示
                segment += "\n\n"
                await show_status(segment + status)
            else:
                await show_status(status)
            working.append(assistant_msg)
            working.extend(await _execute_tool_calls(assistant_msg))
    except Exception:
        logger.exception("调用 LLM 失败")
        try:
            if segment.strip():
                # 已有部分内容：保留定稿，错误另发一条
                await push(segment, final=True)
                await msg.reply_text(t("llm_failed", url=LLM_BASE_URL))
            elif sent is not None and not finalized:
                await sent.edit_text(t("llm_failed", url=LLM_BASE_URL))
            else:
                await msg.reply_text(t("llm_failed", url=LLM_BASE_URL))
        except TelegramError:
            pass
        return None, ""

    if segment.strip():
        await push(segment, final=True)
        finalized += segment
    elif not finalized:
        # 全程没有正文：把占位消息改成空回复提示
        try:
            if sent is not None:
                await sent.edit_text(t("empty_reply"))
        except TelegramError:
            pass
        return None, ""
    return sent, finalized.strip()


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
    if SEARCH_ENABLED:
        logger.info(
            "联网搜索已开启：provider=%s（web_search + open_url）", ",".join(ACTIVE_PROVIDERS)
        )
    skipped = [p for p in SEARCH_PROVIDERS if p not in ACTIVE_PROVIDERS]
    if skipped:
        logger.warning(
            "搜索源 %s 配置不完整或名称不识别（tavily 需 TAVILY_API_KEY，searxng 需 SEARXNG_BASE_URL），已跳过",
            ",".join(skipped),
        )
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
