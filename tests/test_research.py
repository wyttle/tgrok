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


def test_one_failing_researcher_is_reported_but_not_fatal():
    config.RESEARCH_MODELS = ["cpa/ok-opus", "fail"]
    outcome = _research()
    assert outcome.ok and outcome.summary == "1 份报告，1 个失败"
    assert "研究员 B（fail）没有交回报告：boom: no credentials" in outcome.content


def test_all_failing_tells_model_to_fall_back():
    config.RESEARCH_MODELS = ["fail", "fail"]
    outcome = _research()
    assert not outcome.ok and "web_search" in outcome.content


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
    assert research.parse_events("\n".join(lines)) == ("结论", {"input": 12, "output": 6})
