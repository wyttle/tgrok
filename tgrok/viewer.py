"""让 Gemini 看/听媒体（GEMINI_MEDIA_MODEL）：watch_media 工具和后台预读共用。

后台预读：群里一出现视频、语音等就先让 Gemini 完整转述一遍，按 Telegram 的 file_unique_id 缓存。
有人问到时转述多半已经好了，直接放进提问里，主模型不用再调工具、多等一轮；还没好时工具接着等这一份，
不重新下载、不重复请求。
"""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from . import config, usage
from .i18n import t

logger = logging.getLogger(__name__)

TIMEOUT = 120  # 下载加 Gemini 处理的总时长上限（秒）
RESULT_CHARS = 6000  # 一份转述的长度上限
MAX_OUTPUT_TOKENS = 8192  # 思考型 Gemini 的思考也算在里面，给少了正文会被截断
JOBS_KEEP = 500  # 缓存的转述份数
CONCURRENCY = 4  # 同时进行的预读数，免得一群人连发视频时把请求一下子全打出去

Fetch = Callable[[], Awaitable[tuple[bytes, str]]]

_jobs: "OrderedDict[str, asyncio.Task]" = OrderedDict()
_used: set[str] = set()  # 转述已经交给过主模型的媒体：之后的 watch_media 按具体问题重新看
_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _semaphore() -> asyncio.Semaphore:
    """每个事件循环一个（测试里每个用例一个循环）。"""
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(CONCURRENCY))
    return _slots[1]


async def _generate(**kwargs):
    """一次 Gemini 媒体模型调用，成败和 token 都记进用量。"""
    from .llm import gemini as llm_gemini

    t0 = time.monotonic()
    try:
        resp = await llm_gemini.gemini_client.aio.models.generate_content(model=config.GEMINI_MEDIA_MODEL, **kwargs)
    except BaseException:
        usage.record("media", config.GEMINI_MEDIA_MODEL, 0, 0, time.monotonic() - t0, ok=False)
        raise
    spent = llm_gemini.usage_of(resp)
    usage.record("media", config.GEMINI_MEDIA_MODEL, spent.input, spent.output, time.monotonic() - t0)
    return resp


async def look(data: bytes, mime: str, what: str, question: str) -> str:
    """一次 Gemini 调用：看/听这段媒体，按问题转述；空回复返回空串。"""
    from .llm import gemini as llm_gemini

    g = llm_gemini.gtypes
    resp = await _generate(
        contents=[g.Part.from_bytes(data=data, mime_type=mime), t("watch_question", question=question)],
        config=g.GenerateContentConfig(system_instruction=t("watch_viewer_prompt", what=what),
                                       max_output_tokens=MAX_OUTPUT_TOKENS),
    )
    return (getattr(resp, "text", None) or "").strip()[:RESULT_CHARS]


async def look_at(fetch: Fetch, what: str, question: str) -> str:
    async with asyncio.timeout(TIMEOUT):
        data, mime = await fetch()
        return await look(data, mime, what, question)



async def check(data: bytes, mime: str, question: str) -> bool | None:
    """快速判断题：图里是不是某样东西。只要一个「是/否」，思考压到最低、输出很短（完整转述要 20~30 秒，
    这个几秒）；模型不支持 thinking_level 时去掉再试。回答看不懂返回 None。"""
    from .llm import gemini as llm_gemini

    g = llm_gemini.gtypes
    contents = [g.Part.from_bytes(data=data, mime_type=mime), question]
    options = dict(system_instruction=t("image_check_system"), max_output_tokens=256)
    for thinking in (g.ThinkingConfig(thinking_level=g.ThinkingLevel.LOW), None):
        try:
            resp = await _generate(contents=contents, config=g.GenerateContentConfig(**options, thinking_config=thinking))
            break
        except Exception as e:
            if thinking is None or "thinking" not in str(e).lower():
                raise
    answer = (getattr(resp, "text", None) or "").strip().lower()
    if answer.startswith(("是", "yes")):
        return True
    if answer.startswith(("否", "no", "不")):
        return False
    return None

async def _prefetch(fetch: Fetch, what: str) -> str:
    async with _semaphore():
        try:
            text = await look_at(fetch, what, t("watch_default"))
        except Exception as e:
            logger.warning("预读媒体失败（%s）：%s: %.200s", what, type(e).__name__, e)
            return ""
    logger.info("预读媒体完成（%s）：%d 字", what, len(text))
    return text


def start(uid: str, fetch: Fetch, what: str) -> asyncio.Task:
    """开始（或复用）一份后台转述；失败时结果是空串。"""
    job = _jobs.get(uid)
    if job is None:
        job = _jobs[uid] = asyncio.create_task(_prefetch(fetch, what))
        while len(_jobs) > JOBS_KEEP:
            _used.discard(_jobs.popitem(last=False)[0])
    _jobs.move_to_end(uid)
    return job


def job(uid: str) -> asyncio.Task | None:
    """这份媒体的后台转述任务；没预读过或被取消了（比如事件循环结束）返回 None。"""
    job = _jobs.get(uid)
    return None if job is None or job.cancelled() else job


def ready(uid: str) -> str | None:
    """已经完成的转述；还没好、失败了或没预读过返回 None。"""
    job = _jobs.get(uid)
    if job is None or not job.done() or job.cancelled():
        return None
    return job.result() or None


def mark_used(uid: str) -> None:
    _used.add(uid)


def used(uid: str) -> bool:
    return uid in _used


def clear() -> None:
    for job in _jobs.values():
        job.cancel()
    _jobs.clear()
    _used.clear()
