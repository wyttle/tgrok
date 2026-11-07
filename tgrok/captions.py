"""群图快读：群里每张图片（照片、图片文件）一出现，就让一个便宜的看图模型（IMAGE_CAPTION_MODEL）在后台
用一两句话说清是什么，写进最近群聊，主模型就知道群里刚才发的图大概是什么；有人问到、回答要靠图里的细节时，
主模型用 view_image 按编号看原图。

图片只登记在内存里（按聊天和消息 id，最多 REFS_KEEP 张），快读结果也只在内存里：最近群聊本来就只留
RECENT_CHAT_MINUTES 分钟，重启后丢了无所谓。
"""

import asyncio
import base64
import logging
import time
from collections import OrderedDict

from telegram import Message

from . import config, llm, media, usage
from .i18n import t

logger = logging.getLogger(__name__)

TIMEOUT = 30  # 下载加快读的总时长上限（秒）
CAPTION_CHARS = 300  # 一条快读的长度上限
REFS_KEEP = 500
CONCURRENCY = 4  # 同时进行的快读数：一群人连发图时不把请求一下子全打出去

_refs: "OrderedDict[tuple[int, int], dict]" = OrderedDict()  # (聊天, 消息 id) → 图片引用 + bot
_jobs: "OrderedDict[str, asyncio.Task]" = OrderedDict()  # file_unique_id → 快读任务（结果是描述，失败为空串）
_adapter = None
_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _semaphore() -> asyncio.Semaphore:
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(CONCURRENCY))
    return _slots[1]


def _caption_adapter():
    global _adapter
    if _adapter is None:
        endpoint = config.caption_endpoint()
        _adapter = llm.make_adapter(endpoint) if endpoint is not None else None
    return _adapter


def photo_entry(m: Message) -> dict | None:
    """要快读和能 view_image 的图：照片和图片文件（贴纸不算，emoji 就够了）。"""
    if m.sticker:
        return None
    return media.image_entry(m)


def observe(bot, m: Message) -> None:
    """登记一条群消息里的图片，配了 IMAGE_CAPTION_MODEL 就在后台快读。私聊不用：私聊的图都直接交给主模型。"""
    if not config.ENABLE_VISION or m.chat.type == "private":
        return
    entry = photo_entry(m)
    if entry is None:
        return
    key = (m.chat_id, m.message_id)
    _refs[key] = {**entry, "bot": bot}
    _refs.move_to_end(key)
    while len(_refs) > REFS_KEEP:
        _refs.popitem(last=False)
    if _caption_adapter() is not None and entry["uid"] not in _jobs:
        _jobs[entry["uid"]] = asyncio.create_task(_caption(_refs[key]))
        while len(_jobs) > REFS_KEEP:
            _jobs.popitem(last=False)[1].cancel()


def viewable(chat_id: int | None, msg_id: int) -> dict | None:
    return _refs.get((chat_id, msg_id))


def ready(uid: str | None) -> str | None:
    """快读好了的描述；还没好、失败了或没快读过返回 None。"""
    job = _jobs.get(uid) if uid else None
    if job is None or not job.done() or job.cancelled() or job.exception() is not None:
        return None
    return job.result() or None


async def data_url(ref: dict) -> str:
    """下载登记过的图片，转成 data URL（按文件头纠正 MIME）。"""
    file = await ref["bot"].get_file(ref["file_id"])
    data = bytes(await file.download_as_bytearray())
    return f"data:{media.sniff_mime(data, ref['mime'])};base64," + base64.b64encode(data).decode()


async def _caption(ref: dict) -> str:
    adapter = _caption_adapter()
    t0 = time.monotonic()
    async with _semaphore():
        try:
            async with asyncio.timeout(TIMEOUT):
                url = await data_url(ref)
                content = [{"type": "text", "text": t("caption_prompt")}, {"type": "image_url", "image_url": {"url": url}}]

                async def ignore(_):
                    pass

                result = await adapter.run_round([{"role": "user", "content": content}], None, ignore)
        except Exception as e:
            logger.warning("快读图片失败（消息 %s）：%s: %.200s", ref["message_id"], type(e).__name__, e)
            usage.record("caption", adapter.model, 0, 0, time.monotonic() - t0, ok=False)
            return ""
    text = " ".join(result.content.split())[:CAPTION_CHARS]
    usage.record("caption", adapter.model, result.usage.input, result.usage.output, time.monotonic() - t0, ok=bool(text))
    logger.info("快读图片（消息 %s，%.1fs）：%s", ref["message_id"], time.monotonic() - t0, text[:80])
    return text


def clear() -> None:
    global _adapter
    _adapter = None
    for job in _jobs.values():
        job.cancel()
    _jobs.clear()
    _refs.clear()
