"""群图快读和 view_image：群里的图先由小模型一句话说清，最近群聊里带编号；有人问到时主模型按编号看原图。"""

import asyncio

from fakes import CTX, NOW, capture_replies, group, mention, run, update_of, user
from telegram import Message, PhotoSize

from tgrok import captions, config, handlers, tg_auth, tools
from tgrok.i18n import t
from tgrok.llm import RoundResult


class _Captioner:
    model = "cheap-vision"

    def __init__(self):
        self.seen = []

    async def run_round(self, history, tools_, on_text):
        self.seen.append(history[0]["content"])
        return RoundResult(content="  一张猫的图，\n配字「周一不想上班」  ")


def test_group_image_is_captioned_and_the_main_model_can_view_it(monkeypatch):
    # 线上：群友先发图、再另发一条 @bot 问图里是什么，bot 只看到一行 [image]，说自己看不到图
    config.ENABLE_VISION = True
    captioner = _Captioner()
    monkeypatch.setattr(captions, "_adapter", captioner)
    asked = capture_replies(monkeypatch)
    tg_auth.allowed_users.add(5)
    chat, alice, bob = group(), user(6, "Alice"), user(5, "Bob")

    async def scenario():
        photo = Message(40, NOW, chat, from_user=alice, photo=[PhotoSize("cat", "cat-u", 512, 512)])
        await handlers.handle_message(update_of(photo), CTX)  # 没叫 bot：只登记、快读
        await asyncio.gather(*captions._jobs.values())
        await handlers.handle_message(update_of(mention(41, chat, bob, "这图写的啥")), CTX)
        plan = tools.plan_round([{"id": "c1", "function": {"name": "view_image", "arguments": '{"image_id": 40}'}},
                                 {"id": "c2", "function": {"name": "view_image", "arguments": '{"image_id": 7}'}}],
                                chat.id)
        return await plan.execute()

    results = run(scenario())
    assert [p["type"] for p in captioner.seen[0]] == ["text", "image_url"]  # 小模型拿到的是图本身
    turn = asked[-1][-1]["content"]
    assert t("recent_image", id=40, text="一张猫的图， 配字「周一不想上班」") in turn  # 最近群聊里有编号和描述
    assert [m["role"] for m in results] == ["tool", "tool", "user"]
    assert results[1]["content"] == t("view_image_unknown")  # 不存在的编号照常失败
    images = [p for p in results[2]["content"] if p["type"] == "image_url"]
    assert len(images) == 1 and images[0]["image_url"]["url"].startswith("data:image/webp;base64,")


def test_without_a_caption_model_images_still_get_a_number():
    config.ENABLE_VISION = True
    chat, alice = group(), user(6, "Alice")

    async def scenario():
        await handlers.handle_message(update_of(Message(50, NOW, chat, from_user=alice,
                                                        photo=[PhotoSize("p", "p-u", 64, 64)])), CTX)
        return captions.viewable(chat.id, 50), dict(captions._jobs)

    ref, jobs = run(scenario())
    assert ref is not None and not jobs  # 能 view_image，但没配快读模型就不快读
