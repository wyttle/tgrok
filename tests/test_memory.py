"""长期记忆：精选条目（增改删、容量、到期）、注入、原始记录检索、后台整理、旁听、遗忘和旧数据迁移。"""

import json
import time
from datetime import datetime, timedelta

import pytest
from fakes import CTX, HIST, NOW, FakeMsg, chunk_text, chunk_tool, group, ns, run, stream_of, update_of, use_model, \
    user
from telegram import Chat, Message, MessageEntity

from tgrok import chat, commands, config, handlers, llm, memory, tg_auth, tools
from tgrok.llm.base import BaseAdapter, RoundResult
from tgrok.memory import consolidate, notes, store

CHAT = -100


def _add(text, target="group", user_id=None, days=None, chat_id=CHAT):
    op = {"action": "add", "target": target, "text": text}
    if user_id is not None:
        op["user_id"] = user_id
    if days is not None:
        op["days"] = days
    return notes.apply(chat_id, [op], "tool", who_of={user_id: f"人{user_id}（id {user_id}）"} if user_id else None)


# ---- 精选条目 ----

def test_entries_render_with_number_age_and_capacity():
    _add("群里约定周五晚上开黑")
    _add("做外贸，爱吃辣", "user", 5)
    _add("在找工作", "user", 5, days=30)
    block = notes.render(CHAT, [5])
    assert "群笔记 [10/1500 字]" in block and "#1 群里约定周五晚上开黑（刚才）" in block
    assert "人5（id 5） 的档案" in block and "做外贸，爱吃辣（刚才）" in block and "在找工作（刚才，还剩 30 天）" in block


def test_expired_entries_are_hidden_and_purged():
    _add("在考驾照", "user", 5, days=1)
    later = time.time() + 2 * 86400
    assert notes.render(CHAT, [5], now=later) == ""
    run(consolidate.tick(now=later))
    assert store.notes(CHAT) == []


def test_batch_over_capacity_applies_nothing_and_lists_entries():
    config.MEMORY_PROFILE_CHARS = 20
    _add("一二三四五六七八九十", "user", 5)
    with pytest.raises(notes.NoteError) as e:
        notes.apply(CHAT, [{"action": "add", "target": "user", "user_id": 5, "text": "甲乙丙丁戊己庚辛壬癸子丑"},
                           {"action": "add", "target": "group", "text": "群里的事"}], "tool")
    assert "超过上限" in str(e.value) and "一二三四五六七八九十" in str(e.value)
    assert [n.text for n in store.notes(CHAT)] == ["一二三四五六七八九十"]  # 整批没执行
    first = store.notes(CHAT)[0].id
    notes.apply(CHAT, [{"action": "remove", "id": first},
                       {"action": "add", "target": "user", "user_id": 5, "text": "甲乙丙丁戊己庚辛壬癸子丑"}], "tool")
    assert [n.text for n in store.notes(CHAT)] == ["甲乙丙丁戊己庚辛壬癸子丑"]


def test_update_remove_duplicates_and_other_chats():
    _add("爱吃辣", "user", 5, days=10)
    note_id = store.notes(CHAT)[0].id
    assert _add("爱吃辣", "user", 5).skipped == ["爱吃辣"]  # 一模一样的不重复加
    notes.apply(CHAT, [{"action": "update", "id": note_id, "text": "爱吃辣，不吃香菜"}], "tool")
    note = store.notes(CHAT)[0]
    assert note.text == "爱吃辣，不吃香菜" and note.expires is not None  # 没给 days 就保留原到期时间
    notes.apply(CHAT, [{"action": "update", "id": note_id, "days": 0}], "tool")
    assert store.notes(CHAT)[0].expires is None  # days=0 改成长期
    with pytest.raises(notes.NoteError):
        notes.apply(-200, [{"action": "remove", "id": note_id}], "tool")  # 别的聊天的条目动不了
    for bad in ({"action": "add", "target": "user", "text": "x"}, {"action": "add", "target": "group", "text": "x" * 201},
                {"action": "update", "id": 999}, {"action": "drop", "id": note_id}):
        with pytest.raises(notes.NoteError):
            notes.apply(CHAT, [bad], "tool")


def test_entry_text_is_cleaned_of_invisible_characters():
    _add("爱\u200b吃\n辣  ")
    assert store.notes(CHAT)[0].text == "爱吃 辣"


# ---- 注入 ----

def test_block_has_group_notes_and_only_the_named_peoples_profiles():
    _add("群里约定周五晚上开黑")
    _add("做外贸", "user", 5)
    _add("是学生", "user", 6)
    _add("在国外", "user", 7)
    memory.record_exchange(CHAT, 5, "阿强（id 5）", "周五几点开", "八点")
    block = memory.memory_block(CHAT, [5, 6])
    assert "开黑" in block and "做外贸" in block and "是学生" in block and "在国外" not in block
    assert "阿强（id 5）：周五几点开（刚才）" in block and "八点" not in block  # 只放谁问了什么
    assert memory.memory_block(-999, [5]) == ""
    config.MEMORY_ENABLED = False
    assert memory.memory_block(CHAT, [5]) == ""


def test_old_questions_are_not_injected():
    store.add_message(CHAT, 5, "老王（id 5）", "ask", "爆仓了，我要跳楼", ts=time.time() - memory.RECENT_SECONDS - 60)
    assert memory.memory_block(CHAT, [5]) == ""


# ---- 接入消息处理 ----

def _replying(monkeypatch, answer="好的"):
    histories = []

    async def fake_reply(msg, history, keep=None):
        histories.append(history)
        return [77], answer

    monkeypatch.setattr(handlers, "stream_reply", fake_reply)
    return histories


def test_quoted_message_is_attributed_to_its_author_not_the_asker(monkeypatch):
    """阿强回复小李的消息并 @bot：模型看到的、记进原始记录的，都要把那句话算在小李头上。"""
    histories = _replying(monkeypatch, "火锅")
    tg_auth.allowed_users.update({5, 6})
    _add("爱吃辣", "user", 5, chat_id=-7)
    _add("刚毕业", "user", 6, chat_id=-7)
    chat_obj, qiang, li = group(-7), user(5, "阿强"), user(6, "小李")
    quoted = Message(40, NOW, chat_obj, from_user=li, text="我昨天捡到一只猫")
    ask = Message(41, NOW, chat_obj, from_user=qiang, text="@bot 这是真的吗", reply_to_message=quoted,
                  entities=[MessageEntity(MessageEntity.MENTION, 0, 4)])
    run(handlers.handle_message(update_of(ask), CTX))
    first = histories[-1][1]["content"]
    assert "爱吃辣" in first and "刚毕业" in first
    assert "发送者：小李（id 6）（不是提问者）" in first and "【提问】阿强（id 5） 回复了上面这条消息，问你：这是真的吗" in first
    log = [(m.kind, m.user_id, m.text) for m in store.search_messages(-7, "")]
    assert log == [("chat", 6, "我昨天捡到一只猫"), ("ask", 5, "（回复了 小李（id 6） 的消息）这是真的吗"),
                   ("reply", 5, "火锅")]
    # 被引用的消息同时也被旁听到过：按 Telegram 消息 id 去重，不会记两遍
    memory.record_exchange(-7, 5, "阿强（id 5）", "再问一次", "好", quoted={
        "user_id": 6, "who": "小李（id 6）", "text": "我昨天捡到一只猫", "msg_id": 40})
    assert [m.text for m in store.search_messages(-7, "捡到一只猫")] == ["我昨天捡到一只猫"]


def test_overheard_messages_are_archived_without_model_calls(monkeypatch):
    histories = _replying(monkeypatch)
    config.MEMORY_OBSERVE_CHATS = {-50}
    tg_auth.allowed_users.add(1005)
    qiang = user(1005, "阿强")
    for i in range(3):
        run(handlers.handle_message(update_of(Message(100 + i, NOW, group(-50), from_user=qiang,
                                                      text=f"我家猫今天第{i}次打翻水杯")), CTX))
    run(handlers.handle_message(update_of(Message(200, NOW, Chat(-51, "supergroup"), from_user=qiang,
                                                  text="没指定的群")), CTX))
    assert not histories
    assert [m.kind for m in store.search_messages(-50, "")] == ["chat"] * 3
    assert store.search_messages(-51, "") == []


# ---- recall ----

def _seed_log():
    now = time.time()
    store.add_message(CHAT, 5, "阿强（id 5）", "ask", "我生日是五月三号", now - 40 * 86400)
    store.add_message(CHAT, 5, "", "reply", "记住了，五月三号", now - 40 * 86400)
    store.add_message(CHAT, 6, "小李（id 6）", "chat", "周五开黑别迟到", now - 2 * 86400)
    store.add_message(CHAT, 6, "小李（id 6）", "chat", "猫又打翻水杯了", now - 3600)
    store.add_message(-200, 5, "阿强（id 5）", "chat", "别的群里的生日", now)


def _recall(**args):
    return run(tools.REGISTRY["recall"].run(args, CHAT))


def test_recall_finds_messages_by_keyword_person_and_time():
    _seed_log()
    hit = _recall(query="五月三号")
    assert hit.ok and hit.summary == "2 条记录"
    assert "阿强（id 5）：我生日是五月三号" in hit.content and "你：记住了，五月三号" in hit.content
    assert "别的群" not in hit.content  # 只查本聊天
    assert "开黑" in _recall(query="开黑").content  # 两个字的词也能找（不走全文索引）
    assert "生日" not in _recall(query="生日", days=30).content
    by_person = _recall(user_id=6)
    assert by_person.summary == "2 条记录" and "猫又打翻" in by_person.content.splitlines()[-1]
    assert _recall(query="不存在的说法").summary == "0 条记录"


def test_old_messages_are_purged_after_retention():
    store.add_message(CHAT, 5, "阿强（id 5）", "chat", "很久以前", time.time() - 100 * 86400)
    store.add_message(CHAT, 5, "阿强（id 5）", "chat", "最近", time.time())
    run(consolidate.tick())
    assert [m.text for m in store.search_messages(CHAT, "")] == ["最近"]


# ---- memory 工具 ----

def test_memory_tool_saves_during_a_reply():
    rounds = []

    async def create(history, has_tools):
        rounds.append(1)
        if len(rounds) == 1:
            return stream_of([chunk_tool(0, "memory", {"operations": [
                {"action": "add", "target": "user", "user_id": 7, "text": "生日是五月三号"}]})])
        return stream_of([chunk_text("记下了。")])

    use_model(create)
    msg = FakeMsg()
    assert run(chat.stream_reply(msg, HIST))[1] == "记下了。"
    assert [(n.user_id, n.text) for n in store.notes(msg.chat_id)] == [(7, "生日是五月三号")]
    assert any("记忆: 记下 1 条" in x and "已更新" in x for x in msg.log)


def test_memory_tool_reports_capacity_error_to_model():
    config.MEMORY_GROUP_CHARS = 5
    outcome = run(tools.REGISTRY["memory"].run(
        {"operations": [{"action": "add", "target": "group", "text": "超过五个字的群笔记"}]}, CHAT))
    assert not outcome.ok and "超过上限" in outcome.content


# ---- 后台整理 ----

class _Curator(BaseAdapter):
    model = "curator"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.inputs = []

    async def run_round(self, history, tools_, on_text):
        self.inputs.append(history)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return RoundResult(content=answer)


def _log(n, chat_id=CHAT, ts=None):
    for i in range(n):
        store.add_message(chat_id, 5, "阿强（id 5）", "chat", f"第{i}条：我在找工作", ts)


def test_consolidation_applies_operations_and_advances():
    _add("旧的群笔记")
    old_id = store.notes(CHAT)[0].id
    _log(config.MEMORY_CONSOLIDATE_MIN)
    curator = _Curator('好的：{"operations": [{"action": "add", "target": "user", "user_id": 5, '
                       '"text": "在找工作", "days": 30}, {"action": "remove", "id": %d}]}' % old_id)
    llm.adapter, llm.fallback_adapter = curator, None
    run(consolidate.tick())
    assert [(n.user_id, n.text) for n in store.notes(CHAT)] == [(5, "在找工作")]
    assert store.notes(CHAT)[0].who == "阿强（id 5）"
    sent = curator.inputs[0][1]["content"]
    assert "旧的群笔记" in sent and "第0条：我在找工作" in sent and "阿强（id 5）" in sent
    run(consolidate.tick(now=time.time() + 3600 * 3))  # 没有新记录：不再调用模型
    assert len(curator.inputs) == 1

def test_consolidation_runs_on_the_gemini_helper_and_falls_back_to_the_main_model():
    _log(config.MEMORY_CONSOLIDATE_MIN)
    helper, main = _Curator('{"operations": [{"action": "add", "target": "group", "text": "群里在聊找工作"}]}'), \
        _Curator('{"operations": []}')
    llm.helper_adapter, llm.adapter, llm.fallback_adapter = helper, main, None
    run(consolidate.tick())
    assert len(helper.inputs) == 1 and not main.inputs  # 不占主模型
    _log(config.MEMORY_CONSOLIDATE_MIN)
    helper.answers.append(RuntimeError("503"))
    run(consolidate.tick(now=time.time() + config.MEMORY_CONSOLIDATE_INTERVAL * 60 + 1))
    assert len(main.inputs) == 1  # 帮手模型失败时主模型兜底



def test_consolidation_waits_for_enough_messages_or_a_day():
    _log(2)
    curator = _Curator('{"operations": []}')
    llm.adapter, llm.fallback_adapter = curator, None
    run(consolidate.tick())
    assert not curator.inputs
    run(consolidate.tick(now=time.time() + consolidate.STALE_SECONDS + 1))
    assert len(curator.inputs) == 1


def test_consolidation_retries_once_on_capacity_error():
    config.MEMORY_GROUP_CHARS = 10
    _log(config.MEMORY_CONSOLIDATE_MIN)
    curator = _Curator('{"operations": [{"action": "add", "target": "group", "text": "这一条群笔记太长了放不下"}]}',
                       '{"operations": [{"action": "add", "target": "group", "text": "短一点"}]}')
    llm.adapter, llm.fallback_adapter = curator, None
    run(consolidate.tick())
    assert [n.text for n in store.notes(CHAT)] == ["短一点"]
    assert "超过上限" in curator.inputs[1][-1]["content"]


def test_failed_consolidation_keeps_records_for_next_interval():
    _log(config.MEMORY_CONSOLIDATE_MIN)
    llm.adapter, llm.fallback_adapter = _Curator(RuntimeError("503")), _Curator("不是 JSON")
    run(consolidate.tick())
    assert store.progress(CHAT)[0] == 0  # 记录没标成已整理
    curator = _Curator('{"operations": []}')
    llm.adapter, llm.fallback_adapter = curator, None
    run(consolidate.tick())
    assert not curator.inputs  # 失败后等一个周期再试，不会每分钟打模型
    run(consolidate.tick(now=time.time() + config.MEMORY_CONSOLIDATE_INTERVAL * 60 + 1))
    assert len(curator.inputs) == 1 and store.progress(CHAT)[0] > 0


# ---- 遗忘 ----

def test_forgetme_removes_profile_and_log_everywhere():
    _add("爱吃辣", "user", 5)
    _add("群里的事")
    _add("别的群的档案", "user", 5, chat_id=-200)
    memory.record_exchange(CHAT, 5, "阿强（id 5）", "我的生日", "五月三号")
    memory.record_exchange(CHAT, 6, "小李（id 6）", "我的猫", "橘猫")
    assert memory.forget_user(5) == 4  # 两条档案 + 一问一答
    assert [n.text for n in store.notes(CHAT)] == ["群里的事"] and store.notes(-200) == []
    assert [m.text for m in store.search_messages(CHAT, "")] == ["我的猫", "橘猫"]


def test_forget_command_removes_listed_entries_or_everything():
    _add("一")
    _add("二")
    first = store.notes(CHAT)[0].id
    msg = FakeMsg()
    replies = msg.log
    update = ns(effective_chat=ns(id=CHAT), effective_message=msg)
    run(commands.cmd_forget(update, ns(args=[f"#{first}"])))
    assert [n.text for n in store.notes(CHAT)] == ["二"] and f"#{first}" in replies[-1]
    run(commands.cmd_forget(update, ns(args=["12345"])))
    assert "没有 #12345" in replies[-1]
    run(commands.cmd_forget(update, ns(args=[])))
    assert store.notes(CHAT) == []


# ---- 旧版迁移 ----

def test_legacy_memory_json_is_migrated_once():
    today = datetime.now(config.BOT_TZ).date()
    legacy = {str(CHAT): {
        "digest": "\n".join([
            f"[长期] 阿强（id 5）是程序员（{(today - timedelta(days=200)).isoformat()}）",
            f"老王在搬家（{(today - timedelta(days=20)).isoformat()}）",
            f"老王在减肥（{(today - timedelta(days=2)).isoformat()}）",
        ]),
        "recent": [{"who": "阿强（id 5）", "q": "周五几点开", "a": "八点", "t": int(time.time()) - 60}],
        "observed": [{"who": "小李（id 6）", "text": "猫又打翻水杯", "t": int(time.time()) - 30}],
    }}
    config.MEMORY_FILE.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    memory.migrate_legacy()
    entries = {n.text: n for n in store.notes(CHAT)}
    assert set(entries) == {"阿强（id 5）是程序员", "老王在减肥"}  # 过期的一时话题不搬
    assert entries["阿强（id 5）是程序员"].expires is None and entries["老王在减肥"].expires is not None
    assert [(m.kind, m.user_id, m.text) for m in store.search_messages(CHAT, "")] == [
        ("ask", 5, "周五几点开"), ("reply", 5, "八点"), ("chat", 6, "猫又打翻水杯")]
    assert not config.MEMORY_FILE.exists() and config.MEMORY_FILE.with_name("memory.json.migrated").exists()
    memory.migrate_legacy()  # 再跑一次什么都不做
    assert len(store.notes(CHAT)) == 2
