"""bot 自带工具：计算器、搜索合并、进度行，以及端到端的工具循环。"""

import json

import pytest
from fakes import HIST, FakeMsg, ns, run, stream_of

from tgrok import calc, chat, config, llm, tools, web
from tgrok.llm.responses import ResponsesAdapter


def _call(i, name, args):
    return {"id": str(i), "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}


def test_calculate_is_exact_and_handles_dates():
    assert calc.evaluate("1299*0.85") == "1104.15"
    assert calc.evaluate("2^10 + 3*(4-1)") == "1033"
    assert calc.evaluate("7/2 >= 3.5") == "true"
    assert calc.evaluate("date('2026-12-25') - date('2026-10-05')") == "81 days"
    assert calc.evaluate("date('2024-02-28') + days(1)").startswith("2024-02-29")


@pytest.mark.parametrize("bad", ["9**9**9", "factorial(5000)", "__import__('os')", "(1).__class__", "'a'*10", "1/0"])
def test_calculate_rejects_unsafe_or_invalid_expressions(bad):
    with pytest.raises(calc.CalcError):
        calc.evaluate(bad)
    assert not run(tools.REGISTRY["calculate"].run({"expression": bad}, None)).ok


def test_grounding_mode_merges_searches_of_one_round(monkeypatch):
    config.GEMINI_SEARCH_MODEL = "gemini-x"
    queries = []

    async def grounded(query):
        queries.append(query)
        return "综述"

    monkeypatch.setattr(web, "run_web_search", grounded)
    plan = tools.plan_round([_call(0, "web_search", {"query": "q1"}), _call(1, "web_search", {"query": "q2"})], None)
    assert [line.text for line in plan.lines] == ["搜索: q1；q2"]
    results = run(plan.execute())
    assert queries == ["q1；q2"] and results[0]["content"] == "综述" and "已合并" in results[1]["content"]


def test_progress_lines_report_each_outcome(monkeypatch):
    async def fetch(url):
        if "bad" in url:
            raise web.WebError("（读取网页失败）")
        return "(正文恰好以括号开头) " + "x" * 1500  # 括号开头的正文不能被当成失败

    monkeypatch.setattr(web, "run_fetch_url", fetch)
    config.SEARCH_ENABLED = True
    calls = [_call(i, "open_url", {"url": u}) for i, u in enumerate(["https://a", "https://bad", "https://c"])]
    calls += [_call(3, "calculate", "{坏的 JSON"), _call(4, "no_such_tool", {})]
    plan = tools.plan_round(calls, None)
    results = run(plan.execute())
    assert [(line.text, line.result) for line in plan.lines] == [
        ("读取 3 个网页", "3.0k 字，1 个失败"), ("计算: ?", "失败"), ("no_such_tool", "失败")]
    assert [r["tool_call_id"] for r in results] == ["0", "1", "2", "3", "4"]
    assert results[1]["content"] == "（读取网页失败）" and "参数无法解析" in results[3]["content"]


def test_tools_offered_follow_config():
    config.SEARCH_ENABLED, config.SLANG_ENABLED, config.MEMORY_ENABLED = False, False, False
    assert [s["function"]["name"] for s in tools.schemas()] == ["calculate", "send_image"]
    config.SEARCH_ENABLED, config.SLANG_ENABLED, config.MEMORY_ENABLED = True, True, True
    assert [s["function"]["name"] for s in tools.schemas()] == [
        "calculate", "remember_slang", "memory", "recall", "web_search", "open_url", "send_image"]


def test_calculate_tool_loop_end_to_end_via_responses():
    """没配搜索源也能用计算；算出的结果回灌给模型，进度里显示算式和结果。"""
    seen = []

    class Responses:
        async def create(self, **kw):
            seen.append(kw)
            if len(seen) == 1:
                call = ns(type="function_call", call_id="c1", name="calculate", arguments="")
                done = ns(type="function_call", call_id="c1", name="calculate", arguments='{"expression":"1299*0.85"}')
                return stream_of([ns(type="response.output_item.added", output_index=0, item=call),
                                  ns(type="response.output_item.done", output_index=0, item=done),
                                  ns(type="response.completed")])
            return stream_of([ns(type="response.output_text.delta", delta="打完折 1104.15。"),
                              ns(type="response.completed")])

    config.SEARCH_ENABLED = False
    llm.adapter = ResponsesAdapter(client=ns(responses=Responses()))
    msg = FakeMsg()
    assert run(chat.stream_reply(msg, HIST))[1] == "打完折 1104.15。"
    names = [tool["name"] for tool in seen[0]["tools"]]
    assert "calculate" in names and "web_search" not in names
    outputs = [i for i in seen[1]["input"] if i.get("type") == "function_call_output"]
    assert outputs == [{"type": "function_call_output", "call_id": "c1", "output": "1299*0.85 = 1104.15"}]
    assert any("计算: 1299*0.85" in x and "= 1104.15" in x for x in msg.log)
