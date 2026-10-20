"""深入调研：多个 omp 研究员并发，报告一起交回；失败、超时和取消时不留下进程。"""

import asyncio
import os
import shlex
import sys
import time
from pathlib import Path

import pytest
from fakes import run

from tgrok import config, tools
from tgrok.tools import research

FAKE_OMP = Path(__file__).with_name("fake_omp.py")


@pytest.fixture(autouse=True)
def fake_omp(monkeypatch, tmp_path):
    parts = [sys.executable, str(FAKE_OMP)]
    config.OMP_BIN = " ".join(shlex.quote(p) for p in parts) if os.name == "posix" else " ".join(parts)
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setenv("HEARTBEAT", str(heartbeat))
    return heartbeat


def _research(task="哈基米是什么梗"):
    return run(tools.REGISTRY["research"].run({"task": task}, None))


def _stays_still(heartbeat: Path) -> bool:
    size = heartbeat.stat().st_size if heartbeat.exists() else 0
    time.sleep(0.5)
    return (heartbeat.stat().st_size if heartbeat.exists() else 0) == size


def test_disabled_without_models():
    config.RESEARCH_MODELS = []
    assert "research" not in [s["function"]["name"] for s in tools.schemas()]


def test_all_reports_come_back_labelled():
    config.RESEARCH_MODELS = ["cpa/ok-opus", "cpa/ok-gemini"]
    outcome = _research()
    assert outcome.ok and outcome.summary == "2 份报告"
    assert "研究员 A（ok-opus" in outcome.content and "ok-opus 的报告：哈基米是什么梗" in outcome.content
    assert "研究员 B（ok-gemini" in outcome.content and "ok-gemini 的报告：哈基米是什么梗" in outcome.content


def test_researcher_reports_each_step_as_it_happens():
    steps = []
    report = run(research.run_researcher("cpa/ok-opus", "哈基米是什么梗", lambda doing, n: steps.append((doing, n))))
    assert report.text == "cpa/ok-opus 的报告：哈基米是什么梗"  # 20 万字的一行事件也读得下
    assert steps == [("思考中", 0), ("搜「哈基米是什么梗」", 1), ("思考中", 1), ("读 example.com/a", 2), ("思考中", 2)]


def test_progress_line_shows_each_researcher_and_backup_outcome():
    config.RESEARCH_MODELS = ["cpa/ok-opus", "blocked"]
    config.RESEARCH_FALLBACK_MODELS = ["fail-backup"]
    plan = tools.plan_round([{"id": "c1", "function": {"name": "research", "arguments": '{"task": "x"}'}}], None)
    run(plan.execute())
    assert plan.lines[0].detail == ["A ok-opus：已交回报告", "B fail-backup：没交回（no credentials）"]


def test_one_failing_researcher_is_reported_but_not_fatal():
    config.RESEARCH_MODELS = ["cpa/ok-opus", "fail"]
    outcome = _research()
    assert outcome.ok and outcome.summary == "1 份报告，1 个失败"
    assert "研究员 B（fail）没有交回报告：boom: no credentials" in outcome.content


def test_all_failing_tells_model_to_fall_back():
    config.RESEARCH_MODELS = ["fail", "fail"]
    outcome = _research()
    assert not outcome.ok and "web_search" in outcome.content


def test_failing_researcher_is_rerun_on_its_backup_model():
    config.RESEARCH_MODELS = ["fail", "server/ok-gemini"]
    config.RESEARCH_FALLBACK_MODELS = ["cpa/ok-opus", "cpa/ok-flash"]
    outcome = _research()
    assert outcome.ok and outcome.summary == "2 份报告"
    assert "研究员 A（ok-opus" in outcome.content and "研究员 B（ok-gemini" in outcome.content  # 成功的不换
    assert "ok-flash" not in outcome.content


def test_backup_failing_too_reports_both_errors():
    config.RESEARCH_MODELS = ["fail", "cpa/ok-opus"]
    config.RESEARCH_FALLBACK_MODELS = ["fail-backup"]  # 只写一个：所有研究员共用
    outcome = _research()
    assert outcome.ok and "fail: boom: no credentials; fail-backup:" in outcome.content


def test_gateway_error_page_is_reported_by_its_title_and_backup_runs():
    config.RESEARCH_MODELS = ["blocked"]
    config.RESEARCH_FALLBACK_MODELS = ["fail-backup"]
    outcome = _research()
    assert "blocked: 403 网站防火墙; fail-backup: boom: no credentials" in outcome.content


def test_researcher_stopped_by_its_own_time_limit_is_not_rerun_on_backup():
    # omp 自己的 --max-time 到点：再跑备选只会让群友再等一整轮
    config.RESEARCH_MODELS = ["aborted"]
    config.RESEARCH_FALLBACK_MODELS = ["cpa/ok-opus"]
    outcome = _research()
    assert not outcome.ok and "aborted: timed out" in outcome.content and "ok-opus" not in outcome.content


def test_missing_omp_binary_is_a_failure_not_a_crash():
    config.RESEARCH_MODELS = ["cpa/ok-opus"]
    config.OMP_BIN = "definitely-not-omp-binary"
    assert not _research().ok


def test_timed_out_researcher_is_killed(monkeypatch, fake_omp):
    config.RESEARCH_MODELS = ["cpa/ok-opus", "slow"]
    config.RESEARCH_TIMEOUT = 1
    monkeypatch.setattr(research, "KILL_GRACE", 0)
    started = time.monotonic()
    outcome = _research()
    assert time.monotonic() - started < 10
    assert outcome.ok and "timed out" in outcome.content
    assert _stays_still(fake_omp)


def test_cancelled_reply_kills_researchers(fake_omp):
    config.RESEARCH_MODELS = ["slow"]

    async def scenario():
        task = asyncio.create_task(tools.REGISTRY["research"].run({"task": "x"}, None))
        while not fake_omp.exists():
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert _stays_still(fake_omp)


def test_parse_events_takes_last_answer_and_sums_usage():
    lines = [
        '{"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "先搜一下"}], '
        '"usage": {"input": 3, "output": 1}}}',
        '{"type": "message_end", "message": {"role": "assistant", "content": [{"type": "tool_call"}], '
        '"usage": {"input": 4, "output": 2}}}',
        '{"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "结论"}], '
        '"usage": {"input": 5, "output": 3}}}',
    ]
    assert research.parse_events("\n".join(lines)) == ("结论", {"input": 12, "output": 6}, "")
