"""配置：环境变量解析、常量、日志与时区初始化。"""

import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import timezone

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:1234/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "local-model")
LLM_API_KEY = os.getenv("LLM_API_KEY", "not-needed")
# 自定义请求的 User-Agent（部分云端网关会校验 UA），留空使用 SDK 默认值
LLM_USER_AGENT = os.getenv("LLM_USER_AGENT", "").strip()
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1024"))


def _opt_float(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else None


# 采样参数：留空跟随后端默认值。推理类模型常只接受默认值，
# 后端明确拒绝时请求层会去掉参数重试并在进程内粘性禁用（见 llm.create_stream）
LLM_TEMPERATURE = _opt_float("LLM_TEMPERATURE")
LLM_TOP_P = _opt_float("LLM_TOP_P")
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))
# 模型支持图片理解（多模态）时设为 true：群友发图或回复图片提问，图片会一并发给模型
ENABLE_VISION = os.getenv("ENABLE_VISION", "false").strip().lower() in ("1", "true", "yes", "on")
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "4"))  # 单次请求最多附带的图片数
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALBUM_CACHE_SIZE = 300  # 缓存的相册（media group）数量上限
# 联网搜索源：tavily / duckduckgo / searxng，留空关闭。可逗号分隔配置多个源，
# 并发聚合结果（如 SEARCH_PROVIDER=tavily,duckduckgo）。开启后模型可通过
# web_search 工具自主搜索，并可用 open_url 工具读取网页正文。
SEARCH_PROVIDERS = [
    p.strip() for p in os.getenv("SEARCH_PROVIDER", "").replace("，", ",").lower().split(",") if p.strip()
]
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip()
SEARXNG_BASE_URL = os.getenv("SEARXNG_BASE_URL", "").strip().rstrip("/")
SEARCH_MAX_RESULTS = int(os.getenv("SEARCH_MAX_RESULTS", "5"))
SEARCH_MAX_ROUNDS = int(os.getenv("SEARCH_MAX_ROUNDS", "3"))  # 单次回答最多执行工具调用的轮数
SEARCH_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "12"))
SEARCH_RESULT_CHAR_LIMIT = 2400  # 单次回灌给模型的搜索结果文本上限（保护小模型上下文）
SEARCH_SNIPPET_LIMIT = 400  # 单条结果摘要的长度上限
FETCH_CHAR_LIMIT = int(os.getenv("FETCH_CHAR_LIMIT", "3500"))  # 单次回灌给模型的网页正文上限
# 流式空闲看门狗：已有正文后连续这么多秒没有新数据，视为生成已完成、主动收尾。
# 部分网关在内容发完后不发结束帧，流会一直空挂到上游超时报错。0 = 关闭
STREAM_IDLE_TIMEOUT = float(os.getenv("STREAM_IDLE_TIMEOUT", "45"))
# open_url 直接抓取失败（反爬 403 / JS 页面 / 正文过少）时，自动改走 Jina Reader 再试
JINA_FALLBACK = os.getenv("JINA_FALLBACK", "true").strip().lower() in ("1", "true", "yes", "on")
JINA_API_KEY = os.getenv("JINA_API_KEY", "").strip()  # 可选，配置后速率限制更宽松
# Gemini 原生搜索模式：改用 google-genai SDK 直连 Gemini API，启用服务端的
# google_search + url_context 内置工具（Google 在服务端完成搜索与读页，精度更高）。
# 开启后 bot 自带的 web_search/open_url 工具循环不再使用；LLM_API_KEY 填 AI Studio key
GEMINI_NATIVE_SEARCH = os.getenv("GEMINI_NATIVE_SEARCH", "false").strip().lower() in ("1", "true", "yes", "on")
# 混合模式：web_search 工具由该 grounding 模型执行（如 gemini-2.5-flash，免费档可用），
# 回复仍用 LLM_MODEL。与 GEMINI_NATIVE_SEARCH 互斥，留空关闭
GEMINI_SEARCH_MODEL = os.getenv("GEMINI_SEARCH_MODEL", "").strip()
# grounding 专用 key：与主模型解耦——主模型走中转站/本地时，grounding 仍用 AI Studio key；
# 留空则复用 LLM_API_KEY（主模型本身就是 Gemini 官方的场景）
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip() or LLM_API_KEY
# Gemini 原生 API 的接口地址：留空连 Google 官方；中转站支持转发 Gemini 原生格式时
# 填中转站地址（原生模式与 grounding 搜索都会走这里）
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "").strip().rstrip("/")
# grounding 配额超限（429）后的冷却秒数：期间 web_search 回退到自带搜索源
GEMINI_SEARCH_COOLDOWN = 600.0
_gemini_search_blocked_until = [0.0]


def _provider_ready(p: str) -> bool:
    if p == "tavily":
        return bool(TAVILY_API_KEY)
    if p == "serper":
        return bool(SERPER_API_KEY)
    if p == "searxng":
        return bool(SEARXNG_BASE_URL)
    return p == "duckduckgo"


ACTIVE_PROVIDERS = [p for p in SEARCH_PROVIDERS if _provider_ready(p)]
# 混合 grounding 模式下即使没配普通搜索源，也要向模型提供 web_search 工具
SEARCH_ENABLED = bool(ACTIVE_PROVIDERS) or bool(GEMINI_SEARCH_MODEL)
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

TG_MESSAGE_LIMIT = 4096
CONVERSATION_CACHE_SIZE = 500
STREAM_EDIT_INTERVAL = 1.5  # 流式输出时编辑消息的最小间隔（秒），避免触发 Telegram 限流
STREAM_SEGMENT_LIMIT = 3400  # 单条消息承载的流式文本上限，超过则另起一条。
# Telegram 上限 4096；MarkdownV2 转义会使文本膨胀 10% 左右，需留足余量
STREAM_CURSOR = " |"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("trafilatura").setLevel(logging.ERROR)  # "discarding data" 等内部告警无诊断价值
logger = logging.getLogger(__name__)
