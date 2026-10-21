"""send_image：模型需要给人看图时发图。给图片直链，或者给关键词让 bot 搜图；图片跟在这条回复后面发出。

图片先攒进本次回复的 outbox（contextvar，由 chat.stream_reply 设置），回复正文定稿后再一起发，
这样图片总在文字之后、也不会在回复被取消或失败时发出去。按关键词搜到的图，配了 GEMINI_MEDIA_MODEL
时先让 Gemini 看一眼是不是对题，挑第一张对题的发。
"""

import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass

from .. import config, media, viewer, web
from ..i18n import t
from . import Outcome, Tool, failed

logger = logging.getLogger(__name__)

MAX_IMAGES = 4  # 一条回复最多附几张
CANDIDATES = 4  # 按关键词搜图时最多下载、检查几张
CAPTION_CHARS = 1024  # Telegram 图片说明上限


@dataclass
class Image:
    data: bytes
    mime: str
    caption: str
    source: str


# 本次回复要附上的图片；None 表示当前不在一次回复里（比如后台任务），不能发图
outbox: ContextVar[list[Image] | None] = ContextVar("outbox", default=None)


def _image_mime(data: bytes) -> str | None:
    """按文件头确认真是 Telegram 能发的图片（jpeg/png/webp/gif），返回 MIME；不是返回 None。
    不信 Content-Type：有的站点会把 HTML 错误页或防盗链提示标成 image/*。"""
    return media.sniff_mime(data, "") or None


async def _download(url: str) -> Image | None:
    try:
        data, _ = await web.fetch_image(url)
    except web.WebError as e:
        logger.info("候选图片下载失败 %s：%s", url, e)
        return None
    mime = _image_mime(data)
    return Image(data, mime, "", url) if mime else None


CHECK_TIMEOUT = 20  # 对题检查最多等几秒；等不到就算没核对上


async def _matches(image: Image, query: str) -> bool | None:
    """让 Gemini 看一眼这张图是不是对题：True 对题、False 不对题、None 不确定（超时、出错或回答看不懂）。"""
    try:
        verdict = await asyncio.wait_for(viewer.check(image.data, image.mime, t("image_check", query=query)),
                                         CHECK_TIMEOUT)
    except Exception as e:
        logger.info("图片对题检查失败（%s）：%s", type(e).__name__, image.source)
        return None
    logger.info("图片对题检查「%s」：%s %s", query, {True: "对题", False: "不对题", None: "看不懂"}[verdict],
                image.source)
    return verdict


async def _find(query: str) -> Image | None:
    """按关键词搜图：并发下载前几张候选。配了 GEMINI_MEDIA_MODEL 时并发让它核对，按搜索排名取第一张
    确认对题的，不等后面的检查；一张都没确认（不对题、超时或出错）就不发，免得发错图。
    没配 Gemini 没法核对，只能用排名最前的那张。"""
    urls = (await web.search_images(query))[:CANDIDATES]
    images = [i for i in await asyncio.gather(*(_download(u) for u in urls)) if i is not None]
    if not config.GEMINI_MEDIA_MODEL:
        return images[0] if images else None
    checks = [asyncio.create_task(_matches(i, query)) for i in images]
    try:
        for image, check in zip(images, checks):
            if await check:
                return image
        return None
    finally:
        for check in checks:
            check.cancel()


class SendImageTool(Tool):
    name = "send_image"
    description = (
        "Send a picture in the chat along with your reply, when showing it helps: someone asks for a picture or "
        "what something looks like, or a photo, map, chart, or meme image says it better than words. Give either "
        "url (a direct link to the image file itself, e.g. one listed under the page's images in an open_url "
        "result; never make one up) or query (keywords to search for a fitting image). The image is sent right "
        "after your text reply; write the reply as usual and don't paste the link."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Direct http(s) link to an image file."},
            "query": {"type": "string", "description": "What the image should show, to search for one."},
            "caption": {"type": "string", "description": "Optional short caption shown under the image."},
        },
    }

    def prompt_note(self) -> str:
        return t("send_image_prompt", n=MAX_IMAGES)

    def label(self, args: dict) -> str:
        what = str(args.get("query") or args.get("caption") or "").strip()
        return t("tool_send_image", what=what[:40] or t("image_kind"))

    async def run(self, args: dict, chat_id: int | None) -> Outcome:
        pending = outbox.get()
        if pending is None:
            return failed(t("image_no_reply"))
        if len(pending) >= MAX_IMAGES:
            return failed(t("image_limit", n=MAX_IMAGES))
        url, query = str(args.get("url") or "").strip(), str(args.get("query") or "").strip()
        caption = str(args.get("caption") or "").strip()[:CAPTION_CHARS]
        if url:
            try:
                data, ctype = await web.fetch_image(url)
            except web.WebError as e:
                return failed(t("image_failed", error=str(e)))
            mime = _image_mime(data)
            if mime is None:
                return failed(t("image_failed", error=t("image_not_image", ctype=ctype or "?")))
            image = Image(data, mime, caption, url)
        elif query:
            image = await _find(query)
            if image is None:
                return failed(t("image_not_found", query=query))
            image.caption = caption
        else:
            return failed(t("image_need_source"))
        pending.append(image)
        logger.info("回复将附图：%s（%d KB）", image.source, len(image.data) // 1024)
        return Outcome(t("image_queued", n=len(pending)), True, t("res_image"))
