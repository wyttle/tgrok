"""一条回复从生成到显示：重试、断流、取消、分段、工具轮、备用模型。"""

import asyncio
import time

import pytest
from fakes import HIST, FakeMsg, FakeSent, chunk_text, chunk_tool, html_text, ns, openai_adapter, run, stream_of, \
    use_model
from telegram.error import BadRequest, TimedOut

from tgrok import bubble, chat, config, llm, logs, rich, usage, web
from tgrok.i18n import STRINGS, t
from tgrok.llm.base import BaseAdapter, RoundResult


def test_stream_dying_before_any_output_is_retried_once():
    calls = []

    async def create(history, has_tools):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("dead")
        return stream_of([chunk_text("答案A")])

    use_model(create)
    _, answer = run(chat.stream_reply(FakeMsg(), HIST))
    assert answer == "答案A" and len(calls) == 2


def test_stream_cut_after_text_counts_as_finished():
    async def create(history, has_tools):
        return stream_of([chunk_text("完整回答")], tail="cut")

    use_model(create)
    assert run(chat.stream_reply(FakeMsg(), HIST))[1] == "完整回答"


def test_quota_error_is_not_retried_and_says_so():
    calls = []

    async def create(history, has_tools):
        calls.append(1)
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    use_model(create)
    msg = FakeMsg()
    assert run(chat.stream_reply(msg, HIST)) == ([], "")
    assert len(calls) == 1 and "配额超限" in msg.sent[0].text


def test_idle_watchdog_finishes_a_stream_that_never_closes():
    config.STREAM_IDLE_TIMEOUT = 0.5

    async def create(history, has_tools):
        return stream_of([chunk_text("答案B")], tail="hang")

    use_model(create)
    assert run(chat.stream_reply(FakeMsg(), HIST))[1] == "答案B"


async def _cancel(parts, canceller_id):
    async def create(history, has_tools):
        return stream_of(parts, tail="hang")

    use_model(create)
    msg = FakeMsg(uid=7)
    task = asyncio.create_task(chat.stream_reply(msg, HIST))
    await asyncio.sleep(0.3)
    gen_id = max(chat.active_generations)
    answers = []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    q = ns(data=f"c:{gen_id}", from_user=ns(id=canceller_id), answer=answer)
    await chat.on_cancel_button(ns(callback_query=q), None)
    if answers != [t("cancel_done")]:
        chat.active_generations[gen_id][0].cancel()
    result = await task
    return msg, answers, result


def test_cancel_before_output_marks_reply_cancelled():
    msg, answers, result = run(_cancel([], 7))
    assert result == ([], "") and msg.sent[0].text == t("cancelled")


def test_cancel_keeps_partial_text():
    msg, _, _ = run(_cancel([chunk_text("部分")], 7))
    assert "部分" in msg.sent[0].text and t("cancelled_suffix") in msg.sent[0].text


def test_only_asker_or_admin_can_cancel():
    _, answers, _ = run(_cancel([], 8))
    assert answers == [t("cancel_denied")]


def test_expired_callback_answer_is_ignored():
    async def stale(text=None):
        raise BadRequest("Query is too old and response timeout expired or query id is invalid")

    run(chat._answer_callback(ns(answer=stale), "x"))


def test_progress_shows_tool_line_then_next_thinking_stage(monkeypatch):
    config.SEARCH_ENABLED = True
    rounds = []

    async def create(history, has_tools):
        rounds.append(1)
        if len(rounds) == 1:
            return stream_of([chunk_tool(0, "web_search", {"query": "世界杯"})])
        return stream_of([chunk_text("最终答案")])

    async def search(query):
        return "[1] a\nu\ns\n\n[2] b\nu\ns"

    monkeypatch.setattr(web, "run_web_search", search)
    use_model(create)
    msg = FakeMsg()
    _, answer = run(chat.stream_reply(msg, HIST))
    stages = STRINGS["zh"]["thinking_stages"]
    assert answer == "最终答案" and msg.log[0] == stages[0] + "…"
    assert any("搜索: 世界杯" in x and "2 条结果" in x and stages[1] in x for x in msg.log)


def test_slow_tool_shows_a_running_timer_and_typing_status(monkeypatch):
    """等待期间计时在走（工具行带自己的计时），聊天里一直显示「正在输入」。"""
    config.SEARCH_ENABLED = True
    monkeypatch.setattr(bubble, "TICK_PRIVATE", 0.05)
    monkeypatch.setattr(bubble, "SHOW_ELAPSED_AFTER", 0)
    rounds = []

    async def create(history, has_tools):
        rounds.append(1)
        if len(rounds) == 1:
            return stream_of([chunk_tool(0, "web_search", {"query": "台风"})])
        return stream_of([chunk_text("答案")])

    async def slow_search(query):
        await asyncio.sleep(0.4)
        return "[1] a\nu\ns"

    monkeypatch.setattr(web, "run_web_search", slow_search)
    use_model(create)
    msg = FakeMsg()
    assert run(chat.stream_reply(msg, HIST))[1] == "答案"
    assert any("搜索: 台风… 0 秒" in x for x in msg.log)
    assert "typing" in msg.actions
    assert bubble.elapsed_text(65) == "1 分 05 秒" and bubble.elapsed_text(7) == "7 秒"


def test_group_progress_updates_are_paced_to_avoid_rate_limits():
    """群里工具开始、出结果、换阶段挨在一起时不连发编辑，等下个刷新周期再显示。"""
    rounds = []

    async def create(history, has_tools):
        rounds.append(1)
        if len(rounds) == 1:
            return stream_of([chunk_tool(0, "calculate", {"expression": "1+1"})])
        return stream_of([chunk_text("等于 2")])

    use_model(create)
    msg = FakeMsg()
    msg.chat = ns(type="supergroup")
    assert run(chat.stream_reply(msg, HIST))[1] == "等于 2"
    assert msg.log[0] == STRINGS["zh"]["thinking_stages"][0] + "…"
    assert not any("计算" in x for x in msg.log)


def test_slow_telegram_edit_does_not_hold_up_the_next_model_round():
    """Telegram 编辑很慢（或被限流在等）时，模型轮次和工具照常往下走，不等那次显示落地。"""
    marks = {}

    class SlowSent(FakeSent):
        async def edit_text(self, text, parse_mode=None, reply_markup=None):
            if reply_markup is not None:  # 过程中的编辑（定稿不带按钮）
                await asyncio.sleep(1.0)
            await super().edit_text(text, parse_mode, reply_markup)

    class SlowMsg(FakeMsg):
        async def reply_text(self, text, parse_mode=None, reply_markup=None):
            fs = SlowSent(self.log)
            fs.text, fs.message_id = text, 1
            self.sent.append(fs)
            return fs

    async def create(history, has_tools):
        loop = asyncio.get_running_loop()
        if "r0" not in marks:
            marks["r0"] = loop.time()
            return stream_of([chunk_tool(0, "calculate", {"expression": "1+1"})])
        marks["r1"] = loop.time()
        return stream_of([chunk_text("2")])

    use_model(create)
    assert run(chat.stream_reply(SlowMsg(), HIST))[1] == "2"
    assert marks["r1"] - marks["r0"] < 0.5



def test_long_output_is_split_without_losing_text():
    """超过富文本单条上限时按段落拆成几条，每条标签完整、都能单独渲染。"""
    big = "\n\n".join("<b>段%d</b> %s" % (i, "内容" * 200) for i in range(100))

    async def create(history, has_tools):
        return stream_of([chunk_text(big)])

    use_model(create)
    msg = FakeMsg()
    ids, answer = run(chat.stream_reply(msg, HIST))
    finals = [s.html for s in msg.sent]
    assert len(finals) > 1 and all(len(html_text(h)) <= rich.RICH_CHARS for h in finals)
    assert all(h.count("<b>") == h.count("</b>") for h in finals)
    assert answer.replace("\n", "") == big.replace("\n", "")
    # 每一段都要能被回复追问：返回全部分段消息的 id
    assert ids == [x.message_id for x in msg.sent]


def test_draft_written_alongside_tool_calls_is_dropped(monkeypatch):
    rounds = []

    async def create(history, has_tools):
        rounds.append(1)
        if len(rounds) == 1:
            return stream_of([chunk_text("草稿正文。"), chunk_tool(0, "calculate", {"expression": "1+1"})])
        return stream_of([chunk_text("终稿正文。")])

    use_model(create)
    assert run(chat.stream_reply(FakeMsg(), HIST))[1] == "终稿正文。"


def test_draft_is_reused_when_final_round_is_empty():
    rounds = []

    async def create(history, has_tools):
        rounds.append(1)
        if len(rounds) == 1:
            return stream_of([chunk_text("唯一草稿"), chunk_tool(0, "calculate", {"expression": "1+1"})])
        return stream_of([])

    use_model(create)
    assert run(chat.stream_reply(FakeMsg(), HIST))[1] == "唯一草稿"


def test_network_error_on_intermediate_edit_does_not_truncate():
    class FlakySent(FakeSent):
        async def edit_text(self, text, parse_mode=None, reply_markup=None):
            if not self.log or self.log[-1] != "flaky":
                self.log.append("flaky")
                raise TimedOut()
            await super().edit_text(text, parse_mode, reply_markup)

    class FlakyMsg(FakeMsg):
        async def reply_text(self, text, parse_mode=None, reply_markup=None):
            fs = FlakySent(self.log)
            fs.text = text
            self.sent.append(fs)
            return fs

    async def create(history, has_tools):
        # 每块之前停一下：让占位消息先落地，首个 delta 才会触发中间编辑
        return stream_of([chunk_text("前半段，"), chunk_text("后半段。")], delay=0.05)

    config.STREAM_EDIT_INTERVAL_PRIVATE = 1.0  # 只有一次中间编辑，失败的正好是它
    use_model(create)
    msg = FlakyMsg()
    assert run(chat.stream_reply(msg, HIST))[1] == "前半段，后半段。"
    assert "flaky" in msg.log and msg.sent[0].text == "前半段，后半段。"


def test_final_edit_timing_out_once_is_retried():
    """定稿编辑超时不能让气泡停在带光标和取消按钮的中间版本上。"""
    class OnceSent(FakeSent):
        failed = False

        async def edit_text(self, text, parse_mode=None, reply_markup=None):
            if reply_markup is None and not OnceSent.failed:  # 定稿不带按钮
                OnceSent.failed = True
                raise TimedOut()
            await super().edit_text(text, parse_mode, reply_markup)

    class OnceMsg(FakeMsg):
        async def reply_text(self, text, parse_mode=None, reply_markup=None):
            fs = OnceSent(self.log)
            fs.text, fs.message_id = text, 1
            self.sent.append(fs)
            return fs

    async def create(history, has_tools):
        return stream_of([chunk_text("完整回答")], delay=0.05)

    use_model(create)
    msg = OnceMsg()
    assert run(chat.stream_reply(msg, HIST))[1] == "完整回答"
    assert OnceSent.failed and msg.sent[0].text == "完整回答"


class _ServerSearchModel(BaseAdapter):
    """像 Gemini 原生那样自己搜索、不走 bot 工具循环的模型。"""
    supports_tool_loop = False

    def __init__(self):
        self.rounds = []

    async def run_round(self, history, tools, on_text):
        self.rounds.append((history, tools))
        await on_text("Gemini 答案")
        return RoundResult(content="Gemini 答案", citations=[{"uri": "https://example.com/a", "title": "来源 A"}])


def test_server_search_model_runs_one_round_and_appends_sources():
    model = _ServerSearchModel()
    llm.adapter = model
    _, answer = run(chat.stream_reply(FakeMsg(), HIST))
    assert len(model.rounds) == 1 and model.rounds[0][1] is None
    assert answer.endswith('来源：\n<a href="https://example.com/a">来源 A</a>')


# ---- 备用模型 ----

async def _dead(history, has_tools):
    raise RuntimeError("503 upstream unavailable")


@pytest.fixture
def fallback_calls():
    seen = []

    async def ok(history, has_tools):
        seen.append(history)
        return stream_of([chunk_text("备用模型的回答")], delay=0.3)

    llm.fallback_adapter = openai_adapter(ok, model="fb-model")
    return seen


def test_primary_error_before_output_switches_to_fallback_and_notes_it(fallback_calls, monkeypatch):
    monkeypatch.setattr(bubble, "TICK_PRIVATE", 0.05)
    use_model(_dead)
    msg = FakeMsg()
    _, answer = run(chat.stream_reply(msg, HIST))
    assert answer == "备用模型的回答"  # 提示只在显示里，不进对话历史
    assert msg.sent[0].text.startswith("备用模型的回答") and "fb-model" in msg.sent[0].text.replace("\\", "")
    assert any("备用" in x for x in msg.log[1:-1])  # 等备用模型期间进度行有提示


def test_usage_records_failed_primary_and_fallback_tokens_and_stats_by_period():
    async def ok(history, has_tools):
        return stream_of([chunk_text("答"), ns(choices=[], usage=ns(prompt_tokens=1500, completion_tokens=20))])

    use_model(_dead)
    llm.fallback_adapter = openai_adapter(ok, model="fb-model")
    req = logs.begin()
    run(chat.stream_reply(FakeMsg(), HIST))
    rows = {(r["model"], r["calls"], r["fails"], r["tin"], r["tout"]) for r in usage.summary(0)}
    assert rows == {("local-model", 1, 1, 0, 0), ("fb-model", 1, 0, 1500, 20)}
    assert (req.rounds, req.tokens_in, req.tokens_out) == (1, 1500, 20)  # 汇总日志行的合计
    report = usage.report()
    assert report.count("fb-model") == 2  # 今天、近 7 天各一行
    later = usage.report(now=time.time() + 2 * 86400)
    assert later.count("fb-model") == 1 and t("stats_empty", name="今天") in later


def test_primary_cut_after_text_does_not_switch(fallback_calls):
    async def create(history, has_tools):
        return stream_of([chunk_text("主模型正文")], tail="cut")

    use_model(create)
    msg = FakeMsg()
    assert run(chat.stream_reply(msg, HIST))[1] == "主模型正文"
    assert not fallback_calls and "fb-model" not in msg.sent[0].text


def test_fallback_after_tool_round_gets_history_without_vendor_fields(fallback_calls):
    rounds = []

    async def create(history, has_tools):
        rounds.append(1)
        if len(rounds) == 1:
            return stream_of([chunk_tool(0, "calculate", {"expression": "6*7"}, sig="SIG")])
        raise RuntimeError("500")

    use_model(create)
    assert run(chat.stream_reply(FakeMsg(), HIST))[1] == "备用模型的回答"
    history = fallback_calls[0]
    calls = [c for m in history for c in m.get("tool_calls") or []]
    assert calls and all("extra_content" not in c for c in calls)
    assert any(m.get("role") == "tool" and "= 42" in m["content"] for m in history)


def test_fallback_failing_too_reports_failure():
    use_model(_dead)
    llm.fallback_adapter = openai_adapter(_dead, model="fb-model")
    msg = FakeMsg()
    assert run(chat.stream_reply(msg, HIST)) == ([], "")
    assert msg.sent[0].text == t("llm_failed")


def test_fallback_without_tool_loop_gets_system_prompt_without_tool_notes():
    from tgrok import prompt

    model = _ServerSearchModel()
    llm.fallback_adapter = model
    use_model(_dead)
    history = [{"role": "system", "content": prompt.system_prompt(True)}, {"role": "user", "content": "u"}]
    assert t("calc_system_prompt") in history[0]["content"]
    run(chat.stream_reply(FakeMsg(), history))
    assert model.rounds[0][0][0]["content"] == prompt.system_prompt(False)
    assert t("calc_system_prompt") not in model.rounds[0][0][0]["content"]
