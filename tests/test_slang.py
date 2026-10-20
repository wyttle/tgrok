"""梗词典：模型记下用法、消息里出现时附上参考、按群隔离、手动教和删。"""

import json

from fakes import CTX, HIST, FakeMsg, capture_replies, chunk_text, chunk_tool, group, mention, ns, run, stream_of, \
    update_of, use_model, user

from tgrok import chat, commands, config, handlers, slang, tg_auth, tools


def test_model_saves_a_usage_with_context_and_example():
    rounds = []

    async def create(history, has_tools):
        rounds.append(1)
        if len(rounds) == 1:
            return stream_of([chunk_tool(0, "remember_slang", {
                "term": "电子榨菜", "meaning": "吃饭时看的下饭视频，不是真的榨菜",
                "context": "说自己吃饭时在看什么", "example": "今天的电子榨菜是老番", "scope": "global"})])
        return stream_of([chunk_text("哦，下饭视频啊。")])

    use_model(create)
    msg = FakeMsg()
    assert run(chat.stream_reply(msg, HIST))[1] == "哦，下饭视频啊。"
    assert any("记住: 电子榨菜" in x and "已记住" in x for x in msg.log)
    saved = json.loads(config.SLANG_FILE.read_text(encoding="utf-8"))["global|电子榨菜"]["senses"][0]
    assert saved["context"] == "说自己吃饭时在看什么" and saved["example"] == "今天的电子榨菜是老番"


def test_bad_arguments_are_not_saved():
    remember = tools.REGISTRY["remember_slang"]
    assert not run(remember.run({"term": "", "meaning": "x", "scope": "chat"}, -60)).ok
    assert not run(remember.run({"term": "啥", "meaning": "", "scope": "chat"}, -60)).ok
    assert not slang._entries


def _asker(monkeypatch):
    captured = capture_replies(monkeypatch)
    tg_auth.allowed_users.add(1007)
    asker = user(1007, "小李")
    ids = iter(range(300, 400))

    def ask(chat_id, text):
        run(handlers.handle_message(update_of(mention(next(ids), group(chat_id), asker, text)), CTX))
        return captured[-1][-1]["content"]

    return ask, asker


def test_known_term_is_attached_only_when_it_appears(monkeypatch):
    ask, _ = _asker(monkeypatch)
    slang.learn("电子榨菜", "吃饭时看的下饭视频", None, scope=slang.GLOBAL)
    assert "下饭视频" in ask(-60, "今天的电子榨菜是啥")  # 通用梗所有群可见
    assert "下饭视频" not in ask(-60, "今天吃啥")


def test_chat_scoped_usage_stays_in_its_chat(monkeypatch):
    ask, asker = _asker(monkeypatch)
    replies = []

    async def reply(text, **kw):
        replies.append(text)

    update = ns(effective_user=asker, effective_chat=group(-60),
                effective_message=ns(text="/learn 老地方=群主家楼下那家烧烤", reply_text=reply))
    run(commands.cmd_learn(update, None))
    assert "老地方" in replies[-1]
    assert "烧烤" in ask(-60, "晚上老地方见")
    assert "烧烤" not in ask(-61, "晚上老地方见")


def test_term_keeps_several_usages_newest_first(monkeypatch):
    ask, _ = _asker(monkeypatch)
    slang.learn("老地方", "群主家楼下那家烧烤", -60)
    slang.learn("老地方", "群里吵架时说的「回老地方」指去私聊单挑", -60, context="两个人吵起来时")
    block = ask(-60, "有本事老地方见")
    assert block.index("单挑") < block.index("烧烤") and "两个人吵起来时" in block
    slang.learn("老地方", "群里吵架时说的「回老地方」指去私聊单挑", -60, context="两个人吵起来时")  # 相同释义不重复
    for i in range(3):
        slang.learn("老地方", f"第{i}种", -60)
    assert len(slang._entries["-60|老地方"]["senses"]) == slang.MAX_SENSES
    assert slang.forget("老地方", -60) and "烧烤" not in ask(-60, "老地方")


def test_single_character_term_matches_only_short_messages(monkeypatch):
    ask, _ = _asker(monkeypatch)
    slang.learn("典", "嘲讽某人的发言老套又经典", -60, context="单发一个字或很短的回复")
    assert "老套又经典" in ask(-60, "太典了")
    assert "老套又经典" not in ask(-60, "字典里查不到这个词怎么办啊各位")
