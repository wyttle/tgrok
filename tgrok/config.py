"""配置：启动时从环境变量（.env）解析出的全部设置和常量。

其它模块一律通过 `config.X` 在用到时读取，不用 `from .config import X` 复制一份：
测试和运行时改了这里的值，所有模块都能看到。
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

PROTOCOLS = ("openai", "responses", "gemini", "claude")
_TRUE = ("1", "true", "yes", "on")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _url(name: str) -> str:
    return _str(name).rstrip("/")


def _bool(name: str, default: bool) -> bool:
    raw = _str(name)
    return raw.lower() in _TRUE if raw else default


def _int(name: str, default: int) -> int:
    raw = _str(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = _str(name)
    return float(raw) if raw else default


def _opt_float(name: str) -> float | None:
    raw = _str(name)
    return float(raw) if raw else None


def _csv(name: str) -> list[str]:
    """逗号分隔的列表（兼容中文逗号），去掉空项。"""
    return [p.strip() for p in _str(name).replace("，", ",").split(",") if p.strip()]


def _id_set(name: str) -> set[int]:
    return {int(x) for x in _csv(name)}


def _opt_json_obj(name: str) -> dict | None:
    raw = _str(name)
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"{name} 不是合法 JSON：{e}")
    if not isinstance(val, dict):
        raise SystemExit(f'{name} 必须是 JSON 对象，例如 {{"reasoning_effort": "low"}}')
    return val


def _protocol(name: str, default: str) -> str:
    raw = _str(name).lower()
    if raw and raw not in PROTOCOLS:
        logger.warning("未知 %s=%s，改用 %s", name, raw, default or "主模型协议")
        return default
    return raw or default


# ---- Telegram ----
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# 超级管理员：随时可用 bot，并可用 /adduser /deluser 管理白名单
ADMIN_USER_IDS = _id_set("ADMIN_USER_IDS")
# 白名单初始值：只在白名单文件不存在时使用，之后以文件为准
ALLOWED_USER_IDS = _id_set("ALLOWED_USER_IDS")

# ---- 主模型 ----
# openai = OpenAI 兼容 /chat/completions；responses = OpenAI Responses API；
# claude = Anthropic Messages 原生；gemini = Gemini 原生（google_search 由服务端执行，不走 bot 的工具循环）。
# openai/responses/claude 用 LLM_BASE_URL；gemini 用 GEMINI_BASE_URL（留空 = Google 官方）
if not _str("LLM_PROTOCOL") and (_bool("CLAUDE_NATIVE", False) or _bool("GEMINI_NATIVE_SEARCH", False)):
    raise SystemExit("CLAUDE_NATIVE / GEMINI_NATIVE_SEARCH 已移除，请改写成 LLM_PROTOCOL=claude 或 LLM_PROTOCOL=gemini")
LLM_PROTOCOL = _protocol("LLM_PROTOCOL", "openai")
LLM_BASE_URL = _url("LLM_BASE_URL")
LLM_MODEL = _str("LLM_MODEL", "local-model")
LLM_API_KEY = _str("LLM_API_KEY", "not-needed")
LLM_USER_AGENT = _str("LLM_USER_AGENT")  # 部分云端网关会校验 UA；留空用 SDK 默认值
MAX_TOKENS = _int("MAX_TOKENS", 1024)
# 采样参数：留空跟随后端默认值；后端拒绝时适配器去掉参数重试，并在进程内不再发送
LLM_TEMPERATURE = _opt_float("LLM_TEMPERATURE")
LLM_TOP_P = _opt_float("LLM_TOP_P")
# 厂商私有参数（thinking、reasoning_effort 等），OpenAI 兼容/Responses/Claude 原样透传，Gemini 原生忽略
LLM_EXTRA_BODY = _opt_json_obj("LLM_EXTRA_BODY")
# 很多部署方式写不了真正的换行，所以字面的 \n 一律当换行；None 表示未设置，用内置提示词
_system_prompt = os.getenv("SYSTEM_PROMPT")
SYSTEM_PROMPT_OVERRIDE = None if _system_prompt is None else _system_prompt.replace("\\n", "\n")

# ---- Gemini ----
GEMINI_BASE_URL = _url("GEMINI_BASE_URL")
# grounding 和看媒体用的 Gemini key：主模型走中转站时仍可用 AI Studio key；留空复用 LLM_API_KEY
GEMINI_API_KEY = _str("GEMINI_API_KEY") or LLM_API_KEY
# 混合模式：web_search 由这个 Gemini 模型带 google_search grounding 执行，回复仍用主模型；留空关闭
GEMINI_SEARCH_MODEL = _str("GEMINI_SEARCH_MODEL")
GEMINI_SEARCH_COOLDOWN = 600.0  # grounding 配额超限（429）后改用普通搜索源的秒数
# 看媒体：视频、GIF、语音、音频（没开视觉时也包括图片）由主模型调用 watch_media 交给这个 Gemini 模型看/听，
# 转述给主模型；留空关闭。同样用 GEMINI_BASE_URL / GEMINI_API_KEY
GEMINI_MEDIA_MODEL = _str("GEMINI_MEDIA_MODEL")

# ---- 备用模型 ----
# 主模型出错且这条回复还没输出正文时，改用备用模型并在回复末尾注明。留空 LLM_FALLBACK_MODEL 不启用；
# 协议、地址、key 留空沿用主模型，额外参数不继承
LLM_FALLBACK_MODEL = _str("LLM_FALLBACK_MODEL")
LLM_FALLBACK_PROTOCOL = _protocol("LLM_FALLBACK_PROTOCOL", "")
LLM_FALLBACK_BASE_URL = _url("LLM_FALLBACK_BASE_URL")
LLM_FALLBACK_API_KEY = _str("LLM_FALLBACK_API_KEY")
LLM_FALLBACK_EXTRA_BODY = _opt_json_obj("LLM_FALLBACK_EXTRA_BODY")


@dataclass(frozen=True)
class LLMEndpoint:
    """一个可调用的模型。base_url 已按协议归一：openai/responses 为完整 /v1 地址，
    claude 为根地址（空 = 官方），gemini 为原生接口根地址（空 = Google 官方）。"""
    protocol: str
    model: str
    base_url: str
    api_key: str
    extra_body: dict | None


def _endpoint(protocol: str, model: str, base: str, api_key: str, extra_body: dict | None,
              gemini_base: str, gemini_key: str) -> LLMEndpoint:
    if protocol == "gemini":
        return LLMEndpoint(protocol, model, gemini_base, gemini_key, extra_body)
    if protocol == "claude":
        # anthropic SDK 自己拼 /v1/messages：剥掉 OpenAI 习惯的 /v1 尾缀
        return LLMEndpoint(protocol, model, base.removesuffix("/v1"), api_key, extra_body)
    return LLMEndpoint(protocol, model, base or "http://localhost:1234/v1", api_key, extra_body)


def primary_endpoint() -> LLMEndpoint:
    """主模型；每次调用按当前属性现算。"""
    return _endpoint(LLM_PROTOCOL, LLM_MODEL, LLM_BASE_URL, LLM_API_KEY, LLM_EXTRA_BODY,
                     GEMINI_BASE_URL, GEMINI_API_KEY)


def fallback_endpoint() -> LLMEndpoint | None:
    """备用模型；未配置 LLM_FALLBACK_MODEL 时为 None。"""
    if not LLM_FALLBACK_MODEL:
        return None
    return _endpoint(
        LLM_FALLBACK_PROTOCOL or LLM_PROTOCOL, LLM_FALLBACK_MODEL,
        LLM_FALLBACK_BASE_URL or LLM_BASE_URL,
        LLM_FALLBACK_API_KEY or LLM_API_KEY,
        LLM_FALLBACK_EXTRA_BODY,
        LLM_FALLBACK_BASE_URL or GEMINI_BASE_URL,
        LLM_FALLBACK_API_KEY or GEMINI_API_KEY,
    )


# ---- Gemini 帮手模型 ----
# 后台杂活（记忆整理）用的 Gemini 模型，走 GEMINI_BASE_URL / GEMINI_API_KEY；留空沿用 GEMINI_MEDIA_MODEL，
# 两个都空就不启用（记忆整理用主模型）
GEMINI_HELPER_MODEL = _str("GEMINI_HELPER_MODEL") or GEMINI_MEDIA_MODEL
# 媒体预读：群里一出现视频、语音等，就先让 GEMINI_MEDIA_MODEL 在后台完整转述，有人问到时直接用
MEDIA_PREFETCH = _bool("MEDIA_PREFETCH", True)
# 最近群聊：bot 收到的群消息存进数据库（只留 RECENT_CHAT_MINUTES 分钟以内的），提问时把这段时间内最近几条附上作背景。0 = 关闭
RECENT_CHAT_COUNT = _int("RECENT_CHAT_COUNT", 15)
RECENT_CHAT_MINUTES = _int("RECENT_CHAT_MINUTES", 60)


def helper_endpoint() -> LLMEndpoint | None:
    if not GEMINI_HELPER_MODEL:
        return None
    return LLMEndpoint("gemini", GEMINI_HELPER_MODEL, GEMINI_BASE_URL, GEMINI_API_KEY, None)


# ---- 工具与联网 ----
SEARCH_MAX_ROUNDS = _int("SEARCH_MAX_ROUNDS", 3)  # 单次回答最多几轮工具调用（所有工具共用）
# 搜索源：tavily / serper / searxng / duckduckgo，逗号分隔可配多个，并发聚合
SEARCH_PROVIDERS = [p.lower() for p in _csv("SEARCH_PROVIDER")]
TAVILY_API_KEY = _str("TAVILY_API_KEY")
SERPER_API_KEY = _str("SERPER_API_KEY")
SEARXNG_BASE_URL = _url("SEARXNG_BASE_URL")
SEARCH_MAX_RESULTS = _int("SEARCH_MAX_RESULTS", 5)
SEARCH_TIMEOUT = _float("SEARCH_TIMEOUT", 12)
SEARCH_RESULT_CHAR_LIMIT = 7200  # 单次回灌给模型的搜索结果文本上限
SEARCH_SNIPPET_LIMIT = 1200  # 单条结果摘要的长度上限
FETCH_CHAR_LIMIT = _int("FETCH_CHAR_LIMIT", 10500)  # 单次回灌给模型的网页正文上限
# open_url 直接抓取失败（反爬 / JS 页面 / 正文过少）时改走 Jina Reader 再试
JINA_FALLBACK = _bool("JINA_FALLBACK", True)
JINA_API_KEY = _str("JINA_API_KEY")
# 深入调研：research 工具把任务同时交给这些模型（omp 的模型名，逗号分隔），每个模型起一个 omp 无界面进程，
# 只开放搜索和读网页；各自的报告一起交给主模型判断。留空关闭
RESEARCH_MODELS = _csv("RESEARCH_MODELS")
# 研究员的备选模型：某个研究员失败（超时除外）就用对应位置的备选再跑一次；只写一个则所有研究员共用。留空不备选
RESEARCH_FALLBACK_MODELS = _csv("RESEARCH_FALLBACK_MODELS")
OMP_BIN = _str("OMP_BIN", "omp")  # omp 可执行文件（可带参数，按 shell 规则拆分）
RESEARCH_TIMEOUT = _int("RESEARCH_TIMEOUT", 150)  # 单个研究员最多跑多少秒，超时强制结束
RESEARCH_THINKING = _str("RESEARCH_THINKING", "low")  # 研究员的思考强度（omp --thinking）
RESEARCH_CONCURRENCY = _int("RESEARCH_CONCURRENCY", 2)  # 同时最多几个调研任务（每个任务按模型数起进程）
# 每个研究员模型常驻一个 omp（每个约 200MB 内存），接到任务立刻开工，省掉每次十几秒的冷启动；关掉则每次现启动
RESEARCH_WARM = _bool("RESEARCH_WARM", True)


def _provider_ready(p: str) -> bool:
    return {"tavily": bool(TAVILY_API_KEY), "serper": bool(SERPER_API_KEY),
            "searxng": bool(SEARXNG_BASE_URL), "duckduckgo": True}.get(p, False)


ACTIVE_PROVIDERS = [p for p in SEARCH_PROVIDERS if _provider_ready(p)]
# 混合 grounding 模式下即使没配普通搜索源，也要向模型提供 web_search 工具
SEARCH_ENABLED = bool(ACTIVE_PROVIDERS) or bool(GEMINI_SEARCH_MODEL)

# ---- 图片 ----
ENABLE_VISION = _bool("ENABLE_VISION", False)  # 模型支持图片输入时开启
MAX_IMAGES = _int("MAX_IMAGES", 4)  # 单次请求最多附带的图片数
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALBUM_CACHE_SIZE = 300  # 缓存的相册（media group）数量上限

# ---- 对话与回复 ----
MAX_HISTORY = _int("MAX_HISTORY", 20)  # 追问时保留的对话条数（不含 system）
CONVERSATION_CACHE_SIZE = 500  # 内存里留多少段（带图片原样）；数据库里的纯文本版本另按天数保留
CONVERSATION_KEEP_DAYS = _int("CONVERSATION_KEEP_DAYS", 7)  # 回复 bot 多少天以内的消息还能接上上下文
# 所有缓存历史的 content 近似字符量上限，不是进程内存硬上限
CONVERSATION_CONTENT_BUDGET = 64_000_000
# 流式空闲看门狗：已有正文后连续这么多秒没有新数据就视为生成完成。0 = 关闭
STREAM_IDLE_TIMEOUT = _float("STREAM_IDLE_TIMEOUT", 45)
# 流式编辑消息的最小间隔（秒）。Telegram 建议同一聊天每秒最多一条、群里每分钟最多约 20 条，
# 群里编辑太勤会被限流（RetryAfter），画面反而卡住
STREAM_EDIT_INTERVAL_PRIVATE = 1.0
STREAM_EDIT_INTERVAL_GROUP = 3.0
STREAM_CURSOR = " ▍"  # 流式输出时挂在正文末尾的光标

# ---- 持久化 ----
WHITELIST_FILE = Path(_str("WHITELIST_FILE") or Path(__file__).with_name("allowed_users.json"))
# 长期记忆（按聊天分开）：
#   原始记录：bot 见到的问答和旁听群的消息存进 SQLite 全文索引，模型用 recall 工具查原话，保留 MEMORY_ARCHIVE_DAYS 天
#   精选条目：群笔记 + 每个人的档案，一条一条存；模型在聊天中用 memory 工具增改删，后台定期按新记录整理
#   新对话开头注入群笔记、提问者和被引用者的档案
MEMORY_ENABLED = _bool("MEMORY_ENABLED", True)
# bot 的 SQLite 数据库：长期记忆，以及（不管记忆开没开）追问用的对话历史、最近群聊和用量统计
MEMORY_DB = Path(_str("MEMORY_DB") or WHITELIST_FILE.with_name("memory.db"))
# 日志文件：每天一个，留 LOG_KEEP_DAYS 天；0 = 只输出到控制台
LOG_DIR = Path(_str("LOG_DIR") or WHITELIST_FILE.with_name("logs"))
LOG_KEEP_DAYS = _int("LOG_KEEP_DAYS", 14)
MEMORY_FILE = Path(_str("MEMORY_FILE") or WHITELIST_FILE.with_name("memory.json"))  # 旧版记忆，首次启动时迁移
MEMORY_GROUP_CHARS = _int("MEMORY_GROUP_CHARS", 4500)  # 每个聊天的群笔记总字数上限
MEMORY_PROFILE_CHARS = _int("MEMORY_PROFILE_CHARS", 1500)  # 每个人在每个聊天的档案总字数上限
MEMORY_ARCHIVE_DAYS = _int("MEMORY_ARCHIVE_DAYS", 90)  # 原始记录保留天数
MEMORY_CONSOLIDATE_INTERVAL = _int("MEMORY_CONSOLIDATE_INTERVAL", 60)  # 每个聊天最多多少分钟整理一次
MEMORY_CONSOLIDATE_MIN = _int("MEMORY_CONSOLIDATE_MIN", 10)  # 攒够这么多条新记录才整理（隔天的零星记录也会整理）
# 静默旁听：这些群里没叫到 bot 的消息也存进原始记录、参与整理（需关闭 BotFather 隐私模式）
MEMORY_OBSERVE_CHATS = _id_set("MEMORY_OBSERVE_CHATS")
# 梗和黑话词典：模型弄明白一个说法后记下来，以后消息里出现就附上释义
SLANG_ENABLED = _bool("SLANG_ENABLED", True)
SLANG_FILE = Path(_str("SLANG_FILE") or WHITELIST_FILE.with_name("slang.json"))
SLANG_MAX_ENTRIES = _int("SLANG_MAX_ENTRIES", 500)

# ---- 语言与时区 ----
BOT_LANG = _str("BOT_LANG", "zh").lower()
if BOT_LANG not in ("zh", "en"):
    BOT_LANG = "zh"
# 每次请求告诉模型现在的真实时间，避免它瞎猜日期
BOT_TZ_NAME = _str("BOT_TZ") or "Asia/Shanghai"
try:
    BOT_TZ = ZoneInfo(BOT_TZ_NAME)
except (ZoneInfoNotFoundError, ValueError):
    # 标准库 timezone.utc 不依赖 tzdata；ZoneInfo("UTC") 缺 tzdata 时同样会抛异常
    logger.warning("无法识别时区 %s，回退到 UTC", BOT_TZ_NAME)
    BOT_TZ_NAME, BOT_TZ = "UTC", timezone.utc
