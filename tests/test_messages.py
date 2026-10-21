"""群消息怎么变成发给模型的对话：说话人、贴纸和图片、相册、追问缓存、时间。"""

from datetime import datetime

from fakes import BOT_USER, CTX, NOW, capture_replies, group, mention, ns, run, update_of, user
from telegram import Message, MessageEntity, MessageOriginUser, PhotoSize, Sticker, Update

from tgrok import app, config, conversations, handlers, media, prompt, recent, tg_auth
from tgrok.i18n import t


def test_current_time_is_appended_to_user_message():
    content = prompt.with_time("你好")
    assert content.startswith("你好") and datetime.now(config.BOT_TZ).strftime("%Y-%m-%d") in content


def test_album_reply_expands_to_whole_group():
    def photo(mid, group_id=None):
        return ns(message_id=mid, chat_id=-100, media_group_id=group_id, photo=[ns(file_id=f"photo{mid}")],
                  document=None)

    for mid in (12, 11, 12):
        media.remember_album(photo(mid, "g1"))
    assert [e["file_id"] for e in media._image_refs(photo(11, "g1"))] == ["photo11", "photo12"]
    assert [e["file_id"] for e in media._image_refs(photo(99, "gX"))] == ["photo99"]


def test_updates_are_processed_concurrently():
    # 否则生成回复期间，取消按钮的回调会被堵住
    assert app.build_application().concurrent_updates > 1


def test_conversation_cache_evicts_by_content_budget():
    config.CONVERSATION_CONTENT_BUDGET = 10
    conversations.remember(1, 1, [{"role": "user", "content": "aaaa"}])
    conversations.remember(1, 2, [{"role": "user", "content": "bbbb"}])
    conversations.remember(1, 1, [{"role": "user", "content": "ccccc"}])  # 覆盖：刷新顺序和计数
    assert list(conversations._cache) == [(1, 2), (1, 1)] and conversations._total == 9
    conversations.remember(1, 3, [{"role": "user", "content": "ddd"}])
    assert list(conversations._cache) == [(1, 1), (1, 3)]
    assert conversations._total == sum(conversations._sizes.values()) == 8
    conversations.clear()
    conversations.remember(2, 1, [{"role": "user", "content": "x" * 20}])  # 单条超预算也留着
    assert list(conversations._cache) == [(2, 1)]


def test_follow_up_context_survives_restart():
    image = [{"type": "text", "text": "这是什么"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
    history = [{"role": "system", "content": "旧的系统提示词"}, {"role": "user", "content": image},
               {"role": "assistant", "content": "一只猫"}]
    conversations.remember(-5, 77, history, user_id=6)
    assert conversations.get(-5, 77) is history  # 重启前：内存里原样（带图）
    for key in list(conversations._cache):  # 模拟重启：内存层清空，数据库还在
        conversations._evict(key)
    restored = conversations.get(-5, 77)
    assert restored[0] == prompt.system_message()  # 系统提示词换成当前的
    assert restored[1:] == [{"role": "user", "content": "这是什么\n[图片]"}, {"role": "assistant", "content": "一只猫"}]
    assert conversations.forget_user(6) == 1 and conversations.get(-5, 77) is None


def test_trimmed_history_starts_with_user():
    # 严格交替的接口和本地模型模板会拒绝以 assistant 开头的对话
    history = [{"role": "system", "content": "s"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(2 * config.MAX_HISTORY + 1)]
    trimmed = conversations.trim(history)
    assert trimmed[0]["role"] == "system" and trimmed[1]["role"] == "user"
    assert trimmed[-1] is history[-1] and len(trimmed) <= config.MAX_HISTORY + 1


def test_reply_to_bot_with_sticker_or_wordless_photo_still_gets_answered(monkeypatch):
    captured = capture_replies(monkeypatch)
    chat, alice = group(), user(999, "Alice")
    bot_msg = Message(10, NOW, chat, from_user=BOT_USER, text="这锅我可不背")
    sticker = Message(11, NOW, chat, from_user=alice, reply_to_message=bot_msg,
                      sticker=Sticker("sfid", "sfuid", 512, 512, False, False, "regular", emoji="🤬"))
    photo = Message(12, NOW, chat, from_user=alice, reply_to_message=bot_msg,
                    photo=[PhotoSize("pfid", "pfuid", 512, 512)])
    assert handlers.MESSAGE_FILTER.check_update(Update(1, message=sticker))

    config.ENABLE_VISION = True
    run(handlers.handle_message(update_of(sticker), CTX))
    content = captured[-1][-1]["content"]
    assert "🤬" in content[0]["text"] and content[1]["image_url"]["url"].startswith("data:image/webp;base64,")

    config.ENABLE_VISION = False
    run(handlers.handle_message(update_of(photo), CTX))
    assert t("media_unseen", what="图片") in captured[-1][-1]["content"]


def test_attached_images_are_credited_to_whoever_sent_them(monkeypatch):
    # 线上：Bocchi 自己发图配「@bot 这谁」，模型却说「你回复的是 xCoFe 发的图」——图挂在消息末尾，没写是谁发的
    captured = capture_replies(monkeypatch)
    config.ENABLE_VISION = True
    tg_auth.allowed_users.add(5)
    chat, bocchi, xcofe = group(), user(5, "Bocchi"), user(6, "xCoFe")
    tag = [MessageEntity(MessageEntity.MENTION, 0, 4)]
    own = Message(1, NOW, chat, from_user=bocchi, caption="@bot 这谁", caption_entities=tag,
                  photo=[PhotoSize("mine", "mine-u", 512, 512)])
    run(handlers.handle_message(update_of(own), CTX))
    text = captured[-1][-1]["content"][0]["text"]
    assert t("images_own", n=1, name="Bocchi（id 5）") in text and "xCoFe" not in text

    theirs = Message(2, NOW, chat, from_user=xcofe, photo=[PhotoSize("its", "its-u", 512, 512)])
    ask = Message(3, NOW, chat, from_user=bocchi, text="@bot 这谁", entities=tag, reply_to_message=theirs)
    run(handlers.handle_message(update_of(ask), CTX))
    text = captured[-1][-1]["content"][0]["text"]
    assert t("quoted_other", author="xCoFe（id 6）") in text  # 只有图的被引用消息也写明发送者
    assert t("images_quoted", n=1, author="xCoFe（id 6）") in text

    both = Message(4, NOW, chat, from_user=bocchi, caption="@bot 哪个好看", caption_entities=tag,
                   photo=[PhotoSize("mine2", "mine2-u", 512, 512)], reply_to_message=theirs)
    run(handlers.handle_message(update_of(both), CTX))
    content = captured[-1][-1]["content"]
    assert t("images_both", total=2, own=1, quoted=1, name="Bocchi（id 5）", author="xCoFe（id 6）") in content[0]["text"]
    assert len(content) == 3  # 文字 + 自己的图在前 + 被引用的图在后


def test_same_name_users_are_told_apart_by_id(monkeypatch):
    captured = capture_replies(monkeypatch)
    tg_auth.allowed_users.update({1001, 1002})
    chat = group()
    for i, who in enumerate((user(1001, "小明", username="ming_a"), user(1002, "小明"))):
        run(handlers.handle_message(update_of(mention(20 + i, chat, who, "我是谁")), CTX))
    first, second = (h[-1]["content"] for h in captured)
    assert "id 1001" in first and "@ming_a" in first and "id 1002" not in first
    assert "id 1002" in second and "id 1001" not in second.split("\n\n")[-2]


def test_follow_up_after_cache_expired_quotes_previous_bot_reply(monkeypatch):
    captured = capture_replies(monkeypatch)
    chat, alice = group(), user(999, "Alice")
    bot_msg = Message(30, NOW, chat, from_user=BOT_USER, text="上次的回答")
    follow = Message(31, NOW, chat, from_user=alice, reply_to_message=bot_msg, text="然后呢")
    run(handlers.handle_message(update_of(follow), CTX))
    history = captured[-1]
    assert [m["role"] for m in history] == ["system", "user"]  # 不伪造 assistant 轮
    assert "上次的回答" in history[1]["content"] and "然后呢" in history[1]["content"]


def test_recent_group_chat_is_attached_with_who_said_what(monkeypatch):
    """群友就刚才的聊天直接问 bot：附上最近群聊，每行写明谁说的、回复谁；追问只附上次回答之后的新消息。"""
    asked = []

    async def fake_reply(msg, history, keep=None):
        asked.append(history)
        return [60], "他俩在争火锅还是烧烤"

    monkeypatch.setattr(handlers, "stream_reply", fake_reply)
    tg_auth.allowed_users.add(5)
    chat, li, wang, qiang = group(), user(6, "小李"), user(7, "老王"), user(5, "阿强")
    first = Message(50, NOW, chat, from_user=li, text="今晚吃火锅")
    for m in (first, Message(51, NOW, chat, from_user=wang, text="烧烤更好", reply_to_message=first)):
        run(handlers.handle_message(update_of(m), CTX))
    run(handlers.handle_message(update_of(mention(52, chat, qiang, "他俩在吵啥")), CTX))
    content = asked[-1][-1]["content"]
    assert "小李（id 6）：今晚吃火锅" in content and "老王（id 7） 回复 小李（id 6）：烧烤更好" in content
    assert content.index("今晚吃火锅") < content.index("烧烤更好") < content.index("他俩在吵啥")
    assert content.count("他俩在吵啥") == 1  # 提问本身不算进最近群聊

    later = datetime.now()
    run(handlers.handle_message(update_of(Message(61, later, chat, from_user=li, text="那就烧烤吧")), CTX))
    bot_reply = Message(60, later, chat, from_user=BOT_USER, text="他俩在争火锅还是烧烤")
    follow = Message(62, later, chat, from_user=qiang, text="最后定了啥", reply_to_message=bot_reply)
    run(handlers.handle_message(update_of(follow), CTX))
    turn = asked[-1][-1]["content"]
    assert "小李（id 6）：那就烧烤吧" in turn and "今晚吃火锅" not in turn


def test_bot_keeps_seeing_its_own_recent_replies_in_a_busy_group(monkeypatch):
    # 线上：群里 28 分钟 99 条消息，最近 15 条只盖住四五分钟，bot 之前的回答又被截到 200 字，接不上自己说过的话
    asked = capture_replies(monkeypatch)
    config.RECENT_CHAT_COUNT = 3
    tg_auth.allowed_users.add(5)
    chat, li, qiang = group(), user(6, "小李"), user(5, "阿强")
    long_answer = "开头。" + "中间细节" * 60 + "结尾的关键数字是 42。"
    recent.add_bot(chat.id, [80], long_answer, "阿强（id 5）", 5)
    for i in range(81, 87):
        run(handlers.handle_message(update_of(Message(i, NOW, chat, from_user=li, text=f"闲聊{i}")), CTX))
    run(handlers.handle_message(update_of(mention(90, chat, qiang, "你刚才说的数字是多少")), CTX))
    content = asked[-1][-1]["content"]
    assert "关键数字是 42" in content  # bot 的回答没被挤掉，也没截掉结尾
    assert "闲聊84" in content and "闲聊83" not in content  # 别人的消息照旧只带最近 3 条
    assert content.index("关键数字") < content.index("闲聊84")  # 按时间先后排


def test_forwarded_message_is_credited_to_its_original_author(monkeypatch):
    captured = capture_replies(monkeypatch)
    tg_auth.allowed_users.add(5)
    chat, li, wang, qiang = group(), user(6, "小李"), user(7, "老王"), user(5, "阿强")
    fwd = Message(70, NOW, chat, from_user=li, text="我中了五百万", forward_origin=MessageOriginUser(NOW, wang))
    ask = Message(71, NOW, chat, from_user=qiang, text="@bot 真的假的", reply_to_message=fwd,
                  entities=[MessageEntity(MessageEntity.MENTION, 0, 4)])
    run(handlers.handle_message(update_of(ask), CTX))
    content = captured[-1][-1]["content"]
    assert "原作者是 老王（id 7），小李（id 6） 只是转发了它" in content and "发送者：小李（id 6）（不是提问者）" in content
