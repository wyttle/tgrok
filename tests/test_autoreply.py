"""关键词直接回复：管理员用 /kv 增删，群友叫到 bot 时正好发了关键词就直接回，不调模型、不记忆。"""

from fakes import CTX, FakeMsg, capture_replies, group, mention, ns, run, update_of, user

from tgrok import autoreply, commands, conversations, handlers, recent, tg_auth
from tgrok.memory import store

ADMIN = user(999, "管理员")  # conftest 里 ADMIN_USER_IDS=999


def _kv(text, replying_to=None):
    msg = FakeMsg(uid=999)
    msg.text = text
    msg.reply_to_message = replying_to
    run(commands.COMMANDS[[c.names[0] for c in commands.COMMANDS].index("kv")](
        ns(effective_user=ADMIN, effective_chat=ns(id=999, type="private"), effective_message=msg), ns(args=[])))
    return msg.log[-1]


def test_keyword_is_answered_without_the_model_or_memory(monkeypatch):
    asked = capture_replies(monkeypatch)
    tg_auth.allowed_users.add(5)
    assert "群规" in _kv("/kv 群规=第一条：别发广告\n第二条：别刷屏")
    chat, alice = group(-80), user(5, "Alice")
    sent = []

    async def fake_send(target, html, plain, markup=None):
        sent.append(plain)
        return []

    monkeypatch.setattr(handlers.rich, "send_rich", fake_send)
    run(handlers.handle_message(update_of(mention(1, chat, alice, "  群规 ")), CTX))
    assert sent == ["第一条：别发广告\n第二条：别刷屏"] and not asked  # 没调模型
    assert store.search_messages(-80, "") == [] and conversations.get(-80, 1) is None  # 没记忆、没对话历史
    assert "别发广告" not in recent.block(-80, 99, since=0)  # bot 的回复没进最近群聊（群友那句照常记）
    run(handlers.handle_message(update_of(mention(2, chat, alice, "群规是啥")), CTX))
    assert len(asked) == 1 and len(sent) == 1  # 不是整句等于关键词：照常交给模型


def test_admin_can_overwrite_delete_and_save_from_a_replied_message():
    source = FakeMsg()
    source.text = "长回复\n第二行"
    _kv("/kv 早安", replying_to=source)
    assert autoreply.lookup("早安") == "长回复\n第二行"
    _kv("/kv 早安＝早个屁 a=b")  # 全角等号也行，回复里的等号原样保留
    assert autoreply.lookup("早安") == "早个屁 a=b"
    assert "早安" in _kv("/kv")
    assert "已删除" in _kv("/kv del 早安") and autoreply.lookup("早安") is None
    assert "用法" in _kv("/kv 只有词没有回复")


def test_non_admin_cannot_manage_keywords():
    msg = FakeMsg(uid=5)
    msg.text = "/kv 群规=随便写"
    cmd = next(c for c in commands.COMMANDS if c.names[0] == "kv")
    run(cmd(ns(effective_user=user(5, "Alice"), effective_chat=ns(id=5, type="private"), effective_message=msg),
            ns(args=[])))
    assert autoreply.lookup("群规") is None and not msg.log
