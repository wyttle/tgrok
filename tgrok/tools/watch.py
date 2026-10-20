"""watch_media：让 Gemini 看/听群里的视频、GIF、语音和音频（主模型看不到图时也包括图片），把转写和画面描述交给主模型。"""

import asyncio
import logging

from .. import config, media, viewer
from ..i18n import t
from . import Outcome, Tool, failed, human_chars

logger = logging.getLogger(__name__)


class WatchMediaTool(Tool):
    name = "watch_media"
    description = (
        "Watch or listen to a video, GIF, round video message, voice message, or audio file "
        "(or an image, if you can't see images) that someone sent in this chat. You can't perceive it "
        "yourself: a Gemini model views it and returns a transcript of everything said plus a description "
        "of what is shown. Pass the media_id from the note in the message and a specific question."
    )
    parameters = {
        "type": "object",
        "properties": {
            "media_id": {"type": "integer", "description": "The media_id given in the message note."},
            "question": {
                "type": "string",
                "description": "What you need to know, e.g. what they say, what happens, where this is.",
            },
        },
        "required": ["media_id", "question"],
    }

    def enabled(self) -> bool:
        return bool(config.GEMINI_MEDIA_MODEL)

    def prompt_note(self) -> str:
        return t("watch_system_prompt")

    def label(self, args: dict) -> str:
        return t("tool_watch", q=(str(args.get("question", "")).strip() or "?")[:48])

    async def run(self, args: dict, chat_id: int | None) -> Outcome:
        try:
            entry = media.watchable(chat_id, int(args.get("media_id")))
        except (TypeError, ValueError):
            entry = None
        if entry is None:
            return failed(t("watch_unknown"))
        question = str(args.get("question", "")).strip() or t("watch_default")
        what = media.describe(entry)
        job = viewer.job(entry["uid"])
        if job is not None and not viewer.used(entry["uid"]):
            # 后台预读已经在看（或看完了）：等这一份完整转述，不重新下载、不重复请求
            viewer.mark_used(entry["uid"])
            try:
                text = await asyncio.wait_for(asyncio.shield(job), viewer.TIMEOUT)
            except TimeoutError:
                text = ""
            if text:
                logger.info("watch_media 用预读的转述（%s）", what)
                return Outcome(text + "\n\n" + t("watch_prefetched_hint"), True, t("res_chars", k=human_chars(len(text))))
        logger.info("Gemini 查看媒体 (%s) %s: %s", config.GEMINI_MEDIA_MODEL, what, question)
        try:
            text = await viewer.look_at(lambda: media.download(entry), what, question)
        except TimeoutError:
            logger.warning("查看媒体超时（%ds）：%s", viewer.TIMEOUT, what)
            return failed(t("watch_failed", error=t("watch_timeout")))
        except Exception as e:
            logger.warning("查看媒体失败：%s: %s", type(e).__name__, str(e)[:200])
            return failed(t("watch_failed", error=type(e).__name__))
        if not text:
            return failed(t("watch_failed", error=t("watch_empty")))
        viewer.mark_used(entry["uid"])
        return Outcome(text, True, t("res_chars", k=human_chars(len(text))))
