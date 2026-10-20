"""消息里的图片、贴纸和相册：给模型的图片 data URL，以及看不到内容时的文字说明。"""

import asyncio
import base64
import logging
from collections import OrderedDict

from telegram import Message

from . import config
from .i18n import t

logger = logging.getLogger(__name__)

# 相册缓存：Telegram 的多图消息（相册）是多条独立消息，仅靠 media_group_id 关联，
# 回复相册时 reply_to_message 只指向第一条。收到相册成员消息时先记下 file_id，
# 之后有人回复相册提问，就能按组取出全部图片。
# key = (chat_id, media_group_id)，value = [{file_id, mime, message_id}]
album_cache: "OrderedDict[tuple[int, str], list[dict]]" = OrderedDict()


def is_image_document(m: Message) -> bool:
    return bool(m.document and (m.document.mime_type or "").startswith("image/"))


def media_note(m: Message) -> str | None:
    """没有文字的媒体消息给模型的说明：贴纸带上它的 emoji（看不到图时也能猜出情绪），
    图片在未开启视觉时说明看不到内容。开启视觉的普通图片返回 None（图片本身会发给模型）。"""
    if m.sticker:
        return t("sticker_note", emoji=f" {m.sticker.emoji}" if m.sticker.emoji else "")
    if not config.ENABLE_VISION and (m.photo or is_image_document(m)):
        return t("image_unseen")
    return None


def image_entry(m: Message) -> dict | None:
    """从单条消息提取图片引用（压缩照片取最大尺寸；图片文件校验大小；
    静态贴纸本身就是 webp 图片，动态/视频贴纸取它的缩略图）。"""
    if m.photo:
        return {"file_id": m.photo[-1].file_id, "mime": "image/jpeg", "message_id": m.message_id}
    if is_image_document(m):
        if m.document.file_size and m.document.file_size > config.MAX_IMAGE_BYTES:
            return None
        return {"file_id": m.document.file_id, "mime": m.document.mime_type, "message_id": m.message_id}
    if m.sticker:
        if not (m.sticker.is_animated or m.sticker.is_video):
            return {"file_id": m.sticker.file_id, "mime": "image/webp", "message_id": m.message_id}
        if m.sticker.thumbnail:
            return {"file_id": m.sticker.thumbnail.file_id, "mime": "image/webp", "message_id": m.message_id}
    return None


def _sniff_mime(data: bytes, fallback: str) -> str:
    """按文件头识别图片格式：贴纸缩略图可能是 webp 也可能是 jpeg，
    声明的 MIME 和实际内容不符时 Claude 等接口会直接拒绝请求。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return fallback


def remember_album(msg: Message) -> None:
    """记录相册成员消息的图片引用（被动收集，与是否 @bot 无关）。"""
    if not msg.media_group_id:
        return
    entry = image_entry(msg)
    if entry is None:
        return
    key = (msg.chat_id, msg.media_group_id)
    group = album_cache.setdefault(key, [])
    if all(e["message_id"] != entry["message_id"] for e in group):
        group.append(entry)
    album_cache.move_to_end(key)
    while len(album_cache) > config.ALBUM_CACHE_SIZE:
        album_cache.popitem(last=False)


def _image_refs(m: Message) -> list[dict]:
    """一条消息关联的全部图片引用：相册成员展开为整组，普通消息取自身。"""
    if m.media_group_id:
        group = album_cache.get((m.chat_id, m.media_group_id))
        if group:
            return sorted(group, key=lambda e: e["message_id"])
    entry = image_entry(m)
    return [entry] if entry else []


async def image_data_urls(bot, *messages: Message | None) -> list[str]:
    """提取消息中的图片（相册自动展开为整组），转为 base64 data URL；未开启视觉时返回空列表。"""
    if not config.ENABLE_VISION:
        return []
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
        return f"data:{_sniff_mime(data, entry['mime'])};base64," + base64.b64encode(data).decode()

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
