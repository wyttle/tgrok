"""配置向导（configure.py）：.env 读写和额外参数解析。"""

import importlib.util
import io
import json

from conftest import ROOT
from dotenv import dotenv_values

_spec = importlib.util.spec_from_file_location("configure", ROOT / "configure.py")
configure = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(configure)


def test_multiline_prompt_round_trips_through_single_line_env():
    prompt = '第一段\n\n# 标题\n- 带"引号"、反斜杠\\和 # 号\n结尾'
    assert configure.parse_env("A=1\nSYSTEM_PROMPT='" + prompt + "'\nB=2\n") == {
        "A": "1", "SYSTEM_PROMPT": prompt, "B": "2"}
    line = configure.env_line("SYSTEM_PROMPT", prompt)
    assert "\n" not in line
    assert configure.parse_env(line + "\n")["SYSTEM_PROMPT"] == prompt
    assert dotenv_values(stream=io.StringIO(line + "\n"))["SYSTEM_PROMPT"] == prompt  # bot 读回来逐字一致


def test_extra_params_parse_and_echo():
    text = "thinking.type=enabled, thinking.budget_tokens=1000, verbosity=low, stream_usage=true"
    want = {"thinking": {"type": "enabled", "budget_tokens": 1000}, "verbosity": "low", "stream_usage": True}
    assert configure.parse_extra_params(text) == want
    assert configure.parse_extra_params(json.dumps(want)) == want
    assert configure.parse_extra_params(configure.format_extra_params(want)) == want
    assert configure.parse_extra_params("no-equals-sign") is None


def test_reasoning_effort_written_in_each_protocols_shape():
    assert configure.effort_body("low", "responses") == {"reasoning": {"effort": "low"}}
    assert configure.effort_body("low", "") == {"reasoning_effort": "low"}
    assert configure.is_reasoning_gpt("openai/gpt-6.1-sol") and not configure.is_reasoning_gpt("gpt-4o")
