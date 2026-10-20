"""抢答：主模型首轮太慢时同时让 Gemini 帮手模型作答，谁先出字用谁。"""

import asyncio

import pytest
from fakes import HIST, FakeMsg, chunk_text, chunk_tool, run, stream_of, use_model

from tgrok import chat, config, llm
from tgrok.llm.base import BaseAdapter, RoundResult


class _Helper(BaseAdapter):
    """像 Gemini 原生那样不走 bot 工具循环的帮手模型：等 delay 秒后吐出 text（或抛出 error）。"""
    name = "gemini"
    supports_tool_loop = False

    def __init__(self, text="帮手的回答", delay=0.0, error=None):
        self.model, self.text, self.delay, self.error = "gem-helper", text, delay, error
        self.histories, self.cancelled = [], False

    async def run_round(self, history, tools, on_text):
        self.histories.append(history)
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error:
            raise self.error
        await on_text(self.text)
        return RoundResult(content=self.text)


@pytest.fixture
def helper():
    config.HEDGE_AFTER = 0.1
    h = llm.helper_adapter = _Helper()
    return h


def _slow_primary(delay, parts=None, error=None):
    calls = []

    async def create(history, has_tools):
        calls.append(history)
        if error:
            await asyncio.sleep(delay)
            raise error
        return stream_of(parts or [chunk_text("主模型的回答")], delay=delay)

    use_model(create)
    return calls


def test_slow_primary_is_beaten_by_the_helper(helper):
    _slow_primary(1.0)
    msg = FakeMsg()
    _, answer = run(chat.stream_reply(msg, HIST))
    assert answer == "帮手的回答"  # 附注只显示，不进历史
    assert "gem-helper" in msg.sent[0].text.replace("\\", "") and "主模型的回答" not in msg.sent[0].text


def test_primary_answering_or_calling_tools_in_time_is_not_hedged(helper):
    rounds = []

    async def create(history, has_tools):
        rounds.append(1)
        if len(rounds) == 1:
            return stream_of([chunk_tool(0, "calculate", {"expression": "1+1"})])
        await asyncio.sleep(0.3)  # 工具之后的轮次再慢也不抢答：帮手拿不到工具结果
        return stream_of([chunk_text("等于 2")])

    use_model(create)
    assert run(chat.stream_reply(FakeMsg(), HIST))[1] == "等于 2"
    assert not helper.histories


def test_primary_speaking_first_after_the_race_starts_wins(helper):
    helper.delay = 1.0
    _slow_primary(0.3)
    msg = FakeMsg()
    assert run(chat.stream_reply(msg, HIST))[1] == "主模型的回答"
    assert helper.cancelled and "gem-helper" not in msg.sent[0].text


def test_primary_failing_during_the_race_leaves_the_helper_answer(helper):
    helper.delay = 0.3
    _slow_primary(0.2, error=RuntimeError("503"))
    assert run(chat.stream_reply(FakeMsg(), HIST))[1] == "帮手的回答"


def test_helper_gets_a_system_prompt_without_tool_notes(helper):
    from tgrok import prompt

    config.SEARCH_ENABLED = True
    _slow_primary(1.0)
    history = prompt.new_conversation({"role": "user", "content": "今天天气"})
    assert "calculate" in history[0]["content"]
    run(chat.stream_reply(FakeMsg(), history))
    assert "calculate" not in helper.histories[0][0]["content"]


def test_cancel_during_the_race_stops_both(helper):
    helper.delay = 5.0
    calls = _slow_primary(5.0)

    async def scenario():
        task = asyncio.create_task(chat.stream_reply(FakeMsg(), HIST))
        await asyncio.sleep(0.3)
        task.cancel()
        return await task

    assert run(scenario()) == ([], "")
    assert calls and helper.cancelled
