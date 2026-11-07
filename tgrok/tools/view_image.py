"""view_image：主模型按编号看群里之前发的图片原图（最近群聊里写成「图片 #编号」，可能带一句快读描述）。"""

import logging

from .. import captions, config
from ..i18n import t
from . import Outcome, Tool, failed

logger = logging.getLogger(__name__)
MAX_PER_ROUND = 4


class ViewImageTool(Tool):
    name = "view_image"
    description = (
        "Look at an image someone posted earlier in this group chat. In the recent chat, images appear as "
        "\"image #<id>\", sometimes with a one-line description written by a quick helper model. That description "
        "only tells you roughly what the image is; when the conversation is about the image or your answer depends "
        "on what's in it (text, numbers, people, details, whether it's edited), call this to see the original."
    )
    parameters = {
        "type": "object",
        "properties": {"image_id": {"type": "integer", "description": "The number after # in the recent chat."}},
        "required": ["image_id"],
    }

    def enabled(self) -> bool:
        return config.ENABLE_VISION

    def prompt_note(self) -> str:
        return t("view_image_prompt")

    def label(self, args: dict) -> str:
        return t("tool_view_image", id=args.get("image_id", "?"))

    async def run_batch(self, args_list: list[dict], chat_id: int | None) -> list[Outcome]:
        if len(args_list) > MAX_PER_ROUND:
            outcomes = await super().run_batch(args_list[:MAX_PER_ROUND], chat_id)
            return outcomes + [failed(t("view_image_too_many", n=MAX_PER_ROUND))] * (len(args_list) - MAX_PER_ROUND)
        return await super().run_batch(args_list, chat_id)

    async def run(self, args: dict, chat_id: int | None) -> Outcome:
        try:
            image_id = int(args.get("image_id"))
        except (TypeError, ValueError):
            return failed(t("view_image_unknown"))
        ref = captions.viewable(chat_id, image_id)
        if ref is None:
            return failed(t("view_image_unknown"))
        try:
            url = await captions.data_url(ref)
        except Exception as e:
            logger.warning("下载图片失败（消息 %s）：%s", image_id, type(e).__name__)
            return failed(t("view_image_failed", error=type(e).__name__))
        return Outcome(t("view_image_attached", id=image_id), True, t("res_image_seen"), images=[url])
