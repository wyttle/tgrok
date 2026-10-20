"""长期记忆：注入预算、后台压缩、淡化、静默旁听。"""

import asyncio
import json
import time
from datetime import datetime, timedelta

from fakes import CTX, NOW, capture_replies, group, run, update_of, user
from telegram import Chat, Message

from tgrok import config, handlers, llm, memory, tg_auth
from tgrok.llm.base import BaseAdapter, RoundResult


class _Summarizer(BaseAdapter):
    model = "sum"

    def __init__(self, text=None, fail=False, gate=None):
        self.text, self.fail, self.gate = text, fail, gate
        self.inputs = []

    async def run_round(self, history, tools, on_text):
        self.inputs.append(history[-1]["content"])
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("503")
        return RoundResult(content=self.text)


def test_injected_memory_fits_budget_and_keeps_newest_questions():
    config.MEMORY_MAX_CHARS = 200  # 每条提问约 90 字：只放得下最新两条
    for i in range(5):
        memory.record(-1, f"群友{i}", f"问题{i} " + "长" * 80, f"回答{i} " + "长" * 150)
    block = memory.memory_block(-1)
    assert len(block.split("\n", 1)[1]) <= config.MEMORY_MAX_CHARS
    assert "群友4" in block and "群友3" in block and "群友2" not in block
    assert "回答4" not in block  # 只注入谁问了什么，不带旧回答，免得模型顺着旧话题讲
    assert block.index("群友3") < block.index("群友4")
    assert memory.memory_block(-2) == ""
    config.MEMORY_ENABLED = False
    assert memory.memory_block(-1) == ""


def test_background_compression_uses_fallback_and_keeps_new_questions():
    async def scenario():
        for i in range(5):
            memory.record(-1, f"群友{i}", f"问题{i}", f"回答{i}")
        # 第 6 条触发后台压缩：主模型失败时用备用模型；压缩期间新增的问答不会被吞掉
        gate = asyncio.Event()
        llm.adapter = _Summarizer(fail=True)
        llm.fallback_adapter = _Summarizer(text="群友0 在做 tgrok\n" + "很长" * 400, gate=gate)
        memory.record(-1, "群友5", "问题5", "回答5")
        await asyncio.sleep(0)
        memory.record(-1, "群友6", "问题6", "回答6")
        gate.set()
        await asyncio.gather(*memory._tasks)

    run(scenario())
    entry = memory._store["-1"]
    assert entry["digest"].startswith("群友0 在做 tgrok") and len(entry["digest"]) <= config.MEMORY_MAX_CHARS // 2
    assert [r["who"] for r in entry["recent"]] == ["群友4", "群友5", "群友6"]
    assert "-1" in json.loads(config.MEMORY_FILE.read_text(encoding="utf-8"))
    assert "群友0 在做 tgrok" in memory.memory_block(-1)


def test_forget_during_compression_discards_the_result():
    async def scenario():
        gate = asyncio.Event()
        llm.adapter, llm.fallback_adapter = _Summarizer(text="不该出现", gate=gate), None
        for i in range(6):
            memory.record(-3, "某人", f"q{i}", f"a{i}")
        await asyncio.sleep(0)
        assert memory.clear(-3)
        gate.set()
        await asyncio.gather(*memory._tasks)

    run(scenario())
    assert "-3" not in memory._store and "不该出现" not in config.MEMORY_FILE.read_text(encoding="utf-8")


def test_old_questions_expire_and_compression_sees_dates():
    memory.record(-9, "老王（id 7）", "爆仓了，我要跳楼", "先别急")
    memory._store["-9"]["recent"][0]["t"] = time.time() - memory.RECENT_TTL - 60
    assert memory.memory_block(-9) == ""  # 只剩过期提问时什么都不注入
    memory.record(-9, "老王（id 7）", "今天吃什么", "火锅")
    block = memory.memory_block(-9)
    assert "今天吃什么" in block and "跳楼" not in block

    probe = _Summarizer(text="老王（id 7）爱吃火锅（10-05）")
    llm.adapter, llm.fallback_adapter = probe, None
    run(memory._summarize(memory._qa_input("", memory._store["-9"]["recent"])))
    old_day = memory._date(memory._store["-9"]["recent"][0]["t"], "%Y-%m-%d")
    assert memory._date(time.time(), "%Y-%m-%d") in probe.inputs[0] and f"[{old_day}]" in probe.inputs[0]


def test_injected_digest_lines_show_age_and_drop_stale_passing_topics():
    today = datetime.now(config.BOT_TZ).date()
    memory._store["-9"] = {"digest": "\n".join([
        f"[长期] 老王（id 7）是程序员（{(today - timedelta(days=200)).isoformat()}）",
        f"老王在搬家（{(today - timedelta(days=20)).isoformat()}）",
        f"老王在减肥（{(today - timedelta(days=2)).isoformat()}）",
        f"老王在学吉他（{(today - timedelta(days=1)).strftime('%m-%d')}）",
        "没写日期的旧条目",
    ]), "recent": [{"who": "老王（id 7）", "q": "今天吃啥", "a": "火锅", "t": int(time.time()) - 2 * 3600}]}
    block = memory.memory_block(-9)
    assert "是程序员（约 6 个月前）" in block  # 长期信息再久也留，标上时间
    assert "搬家" not in block  # 超过 MEMORY_FRESH_DAYS 的一时话题不注入
    assert "在减肥（2 天前）" in block and "在学吉他（昨天）" in block and "没写日期的旧条目" in block
    assert "今天吃啥（2 小时前）" in block


class _ObservedSummarizer(BaseAdapter):
    model = "obs"

    def __init__(self, calls, fail=False):
        self.calls, self.fail = calls, fail

    async def run_round(self, history, tools, on_text):
        self.calls.append(history[-1]["content"])
        if self.fail:
            raise RuntimeError("503")
        memory.observe(-50, "后来者（id 9）", "整理期间的新消息")
        return RoundResult(content="阿强（id 5）在养猫（10-05）")


def test_overheard_messages_are_batched_and_summarized_periodically(monkeypatch):
    replies = capture_replies(monkeypatch)
    config.MEMORY_OBSERVE_CHATS, config.MEMORY_OBSERVE_INTERVAL = {-50}, 60
    tg_auth.allowed_users.add(1005)
    qiang = user(1005, "阿强")
    for i in range(6):
        msg = Message(100 + i, NOW, group(-50), from_user=qiang, text=f"我家猫今天第{i}次打翻水杯")
        run(handlers.handle_message(update_of(msg), CTX))
    run(handlers.handle_message(update_of(Message(200, NOW, Chat(-51, "supergroup"), from_user=qiang,
                                                  text="没指定的群")), CTX))
    calls = []
    assert not replies  # 旁听不回复
    assert memory.pending_observed(-50) == 6 and memory.pending_observed(-51) == 0
    assert "阿强（id 1005）" in memory._store["-50"]["observed"][0]["who"]

    start = memory._store["-50"]["observed_at"]
    llm.adapter, llm.fallback_adapter = _ObservedSummarizer(calls), None
    run(memory.observe_tick(now=start + 59 * 60))  # 还没满一个周期
    assert not calls
    run(memory.observe_tick(now=start + 61 * 60))
    assert len(calls) == 1 and "我家猫今天第5次打翻水杯" in calls[0]
    assert memory._store["-50"]["digest"].startswith("阿强（id 5）在养猫")
    assert [r["text"] for r in memory._store["-50"]["observed"]] == ["整理期间的新消息"]  # 整理期间来的不被吞

    # 整理失败：缓冲区原样保留，且要等下个周期才重试（不会每分钟打一次模型）
    for i in range(5):
        memory.observe(-50, "阿强（id 1005）", f"又一条{i}")
    llm.adapter = _ObservedSummarizer(calls, fail=True)
    later = memory._store["-50"]["observed_at"] + 61 * 60
    run(memory.observe_tick(now=later))
    run(memory.observe_tick(now=later + 60))
    assert len(calls) == 2 and memory.pending_observed(-50) == 6

    # 缓冲区按字数封顶，丢最旧的
    config.MEMORY_OBSERVE_MAX_CHARS = 30
    memory.observe(-50, "阿强（id 1005）", "很长" * 10)
    assert sum(len(r["text"]) for r in memory._store["-50"]["observed"]) <= 30
