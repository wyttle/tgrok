"""视频、语音等交给 Gemini 看/听：消息里的说明、watch_media 工具、看不了时的降级、旁听记录。"""

import pytest
from fakes import BOT_USER, CTX, NOW, FakeMsg, capture_replies, chunk_text, chunk_tool, group, ns, run, stream_of, \
    update_of, use_model, user
from telegram import Message, MessageEntity, Update, Video, Voice

from tgrok import chat, config, handlers, media, tg_auth, tools
from tgrok.i18n import t
from tgrok.llm import gemini as llm_gemini
from tgrok.memory import store

CHAT = -100  # 和 FakeMsg 的 chat_id 一致：工具按（聊天, media_id）取媒体


@pytest.fixture
def gemini(monkeypatch):
    """假的 Gemini 媒体模型：记下每次请求，按 replies 依次回答（异常就抛出）。"""
    calls, replies = [], []

    async def generate_content(model, contents, config):
        calls.append({"model": model, "contents": contents, "config": config})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ns(text=reply)

    monkeypatch.setattr(llm_gemini, "gemini_client", ns(aio=ns(models=ns(generate_content=generate_content))))
    monkeypatch.setattr(llm_gemini, "gtypes", ns(Part=ns(from_bytes=lambda data, mime_type: (data, mime_type)),
                                                 GenerateContentConfig=lambda **kw: kw))
    config.GEMINI_MEDIA_MODEL = "gemini-x"
    return calls, replies


def test_voice_reply_is_heard_by_gemini_and_retold_to_main_model(monkeypatch, gemini):
    calls, replies = gemini
    replies.append("她说：明天八点集合。")
    captured = capture_replies(monkeypatch)
    chat_obj = group(CHAT)
    bot_msg = Message(10, NOW, chat_obj, from_user=BOT_USER, text="几点集合？")
    voice = Message(11, NOW, chat_obj, from_user=user(999, "Alice"), reply_to_message=bot_msg,
                    voice=Voice("vfid", "vuid", 8, mime_type="audio/ogg"))
    assert handlers.MESSAGE_FILTER.check_update(Update(1, message=voice))
    run(handlers.handle_message(update_of(voice), CTX))
    history = captured[-1]
    assert t("media_watchable", what="语音（8 秒）", id=11) in history[-1]["content"]

    rounds = []

    async def create(working, has_tools):
        rounds.append(working)
        if len(rounds) == 1:
            return stream_of([chunk_tool(0, "watch_media", {"media_id": 11, "question": "说了啥"})])
        return stream_of([chunk_text("明天八点。")])

    use_model(create)
    msg = FakeMsg()
    assert run(chat.stream_reply(msg, history))[1] == "明天八点。"
    (call,) = calls
    data, mime = call["contents"][0]
    assert call["model"] == "gemini-x" and mime == "audio/ogg" and data.startswith(b"RIFF")
    assert "说了啥" in call["contents"][1] and "语音（8 秒）" in call["config"]["system_instruction"]
    assert rounds[1][-1]["content"] == "她说：明天八点集合。"
    assert any("查看媒体: 说了啥" in x for x in msg.log)


def test_without_media_model_caption_is_kept_and_video_marked_unseen(monkeypatch):
    captured = capture_replies(monkeypatch)
    tg_auth.allowed_users.add(5)
    chat_obj = group(CHAT)
    video = Message(20, NOW, chat_obj, from_user=user(6, "小李"), caption="笑死", video=Video("vid", "vuid", 640, 360, 12))
    ask = Message(21, NOW, chat_obj, from_user=user(5, "阿强"), text="@bot 这是真的吗", reply_to_message=video,
                  entities=[MessageEntity(MessageEntity.MENTION, 0, 4)])
    run(handlers.handle_message(update_of(ask), CTX))
    content = captured[-1][-1]["content"]
    assert "笑死" in content and t("media_unseen", what="视频（12 秒）") in content and "这是真的吗" in content
    assert "watch_media" not in [s["function"]["name"] for s in tools.schemas()]
    assert not media.watch_cache


def test_oversized_unknown_or_failed_media_is_reported_as_not_viewed(gemini):
    calls, replies = gemini
    watch = tools.REGISTRY["watch_media"]
    big = Message(30, NOW, group(CHAT), from_user=user(6, "小李"),
                  video=Video("big", "bu", 640, 360, 300, file_size=30 * 1024 * 1024))
    assert media.media_note(big) == t("media_too_large", what="视频（300 秒）")
    media.offer_to_watch(CTX.bot, big)
    assert run(watch.run({"media_id": 30, "question": "拍的啥"}, CHAT)).content == t("watch_unknown")

    clip = Message(31, NOW, group(CHAT), from_user=user(6, "小李"), video=Video("v", "u", 640, 360, 5))
    media.offer_to_watch(CTX.bot, clip)
    assert run(watch.run({"media_id": 31, "question": "拍的啥"}, -999)).content == t("watch_unknown")  # 别的聊天拿不到
    replies.append(RuntimeError("503"))
    outcome = run(watch.run({"media_id": 31, "question": "拍的啥"}, CHAT))
    assert not outcome.ok and outcome.content == t("watch_failed", error="RuntimeError")


def test_overheard_voice_is_logged_with_a_tag():
    config.MEMORY_OBSERVE_CHATS = {CHAT}
    voice = Message(40, NOW, group(CHAT), from_user=user(6, "小李"), voice=Voice("v", "u", 8))
    run(handlers.handle_message(update_of(voice), CTX))
    assert [m.text for m in store.search_messages(CHAT, "")] == ["[语音（8 秒）]"]
