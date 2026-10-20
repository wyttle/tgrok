"""消息里的媒体：给模型的图片 data URL、交给 Gemini 看/听的视频语音、看不到内容时的文字说明，以及相册缓存。"""

import asyncio
import base64
import logging
from collections import OrderedDict

from telegram import Message

from . import config, llm
from .i18n import phrases, t

logger = logging.getLogger(__name__)

# 相册缓存：Telegram 的多图消息（相册）是多条独立消息，仅靠 media_group_id 关联，
# 回复相册时 reply_to_message 只指向第一条。收到相册成员消息时先记下 file_id，
# 之后有人回复相册提问，就能按组取出全部图片。
# key = (chat_id, media_group_id)，value = [{file_id, mime, message_id}]
album_cache: "OrderedDict[tuple[int, str], list[dict]]" = OrderedDict()

# 主模型看不到/听不到、但可以交给 GEMINI_MEDIA_MODEL 的媒体：bot 被叫到时登记，watch_media 工具按
# media_id（就是消息 id）取出来下载。key = (chat_id, message_id)，value = media_entry() + bot
watch_cache: "OrderedDict[tuple[int, int], dict]" = OrderedDict()
WATCH_CACHE_SIZE = 300
MAX_MEDIA_BYTES = 20 * 1024 * 1024  # 官方 Bot API 只能下载 20MB 以内的文件


def is_image_document(m: Message) -> bool:
    return bool(m.document and (m.document.mime_type or "").startswith("image/"))


def media_entry(m: Message) -> dict | None:
    """视频、GIF、圆形视频、语音、音频（包括以文件发送的）；没开视觉时也包括图片。其它返回 None。"""
    doc_mime = (m.document.mime_type or "") if m.document else ""
    if m.animation:  # GIF 在 Telegram 里是 mp4，animation 消息同时带 document，要先判断
        obj, kind, mime = m.animation, "gif", "video/mp4"
    elif m.video:
        obj, kind, mime = m.video, "video", "video/mp4"
    elif m.video_note:
        obj, kind, mime = m.video_note, "video_note", "video/mp4"
    elif m.voice:
        obj, kind, mime = m.voice, "voice", "audio/ogg"
    elif m.audio:
        obj, kind, mime = m.audio, "audio", "audio/mpeg"
    elif doc_mime.startswith(("video/", "audio/")):
        obj, kind, mime = m.document, doc_mime.split("/")[0], doc_mime
    elif not config.ENABLE_VISION and m.photo:
        obj, kind, mime = m.photo[-1], "image", "image/jpeg"
    elif not config.ENABLE_VISION and is_image_document(m):
        obj, kind, mime = m.document, "image", doc_mime
    else:
        return None
    # to_dict 取时长：直接读 .duration 会触发 PTB 的弃用警告（以后改成 timedelta）
    return {"kind": kind, "file_id": obj.file_id, "mime": getattr(obj, "mime_type", None) or mime,
            "seconds": obj.to_dict().get("duration"), "size": obj.file_size, "message_id": m.message_id}


def describe(entry: dict) -> str:
    """给模型看的媒体名称，带时长：语音（8 秒）。"""
    what = phrases("media_kinds")[entry["kind"]]
    return t("media_duration", what=what, n=int(entry["seconds"])) if entry["seconds"] else what


def can_watch() -> bool:
    """watch_media 只在配了 Gemini 媒体模型、且主模型走 bot 工具循环时可用。"""
    return bool(config.GEMINI_MEDIA_MODEL) and llm.adapter.supports_tool_loop


def _too_large(entry: dict) -> bool:
    return bool(entry["size"]) and entry["size"] > MAX_MEDIA_BYTES


def media_note(m: Message) -> str | None:
    """媒体消息给模型的说明：视频、语音等交给 Gemini 看时给出 media_id，看不了就明说看不到；
    贴纸带上它的 emoji（看不到图时也能猜出情绪）。开启视觉的普通图片返回 None（图片本身会发给模型）。"""
    entry = media_entry(m)
    if entry:
        what = describe(entry)
        if _too_large(entry):
            return t("media_too_large", what=what)
        if can_watch():
            return t("media_watchable", what=what, id=entry["message_id"])
        return t("media_unseen", what=what)
    if m.sticker:
        return t("sticker_note", emoji=f" {m.sticker.emoji}" if m.sticker.emoji else "")
    return None


def offer_to_watch(bot, *messages: Message | None) -> None:
    """登记这次提问涉及的媒体（提问消息本身和被引用的消息），供 watch_media 按 media_id 取用。"""
    if not can_watch():
        return
    for m in messages:
        entry = media_entry(m) if m is not None else None
        if entry and not _too_large(entry):
            key = (m.chat_id, m.message_id)
            watch_cache[key] = {**entry, "bot": bot}
            watch_cache.move_to_end(key)
    while len(watch_cache) > WATCH_CACHE_SIZE:
        watch_cache.popitem(last=False)


def watchable(chat_id: int | None, media_id: int) -> dict | None:
    return watch_cache.get((chat_id, media_id))


async def download(entry: dict) -> tuple[bytes, str]:
    """下载登记过的媒体，返回（内容, MIME）；图片按文件头纠正 MIME。"""
    file = await entry["bot"].get_file(entry["file_id"])
    data = bytes(await file.download_as_bytearray())
    mime = _sniff_mime(data, entry["mime"]) if entry["kind"] == "image" else entry["mime"]
    return data, mime


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
