"""协议适配器：历史格式转换、流消费、后端拒绝参数时的降级。"""

import json

import anthropic
from fakes import HIST, bad_request, chunk_tool, ns, openai_adapter, run, stream_of
from openai import BadRequestError

from tgrok import config, llm, tools
from tgrok.llm import claude, gemini, openai, responses
from tgrok.llm.base import rejected_param
from tgrok.llm.claude import ClaudeAdapter
from tgrok.llm.openai import OpenAIAdapter


async def _ignore(text):
    pass


def _recording(*failures, result="S"):
    """一个假的 create：前几次按顺序抛出 failures，之后返回 result；seen 记录每次请求参数。"""
    seen = []

    async def create(**kw):
        seen.append(kw)
        if len(seen) <= len(failures):
            raise failures[len(seen) - 1]
        return result

    return seen, create


def test_sdk_retries_are_disabled():
    # SDK 内建重试会按 Retry-After 静默睡 60s+，取消和自有重试全部失效
    assert OpenAIAdapter().client.max_retries == 0
    assert ClaudeAdapter().client.max_retries == 0


def test_tool_names_renamed_by_the_gateway_are_mapped_back():
    # 线上：cpa 把工具改名成 mcp__warrior_normal__crunch_research 发给模型，还原时偶尔只去掉 mcp__…__，
    # 交回 crunch_research、identify_web_search，整轮工具 failed
    async def create(messages, has_tools):
        return stream_of([chunk_tool(0, "crunch_research", {"task": "x"}), chunk_tool(1, "identify_web_search", {"query": "q"}),
                          chunk_tool(2, "mcp__warrior_normal__imitate_calculate", {}), chunk_tool(3, "made_up_tool", {})])

    offered = [{"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
               for n in ("research", "web_search", "calculate")]
    result = run(openai_adapter(create).run_round(HIST, offered, _ignore))
    assert [result.calls[i]["name"] for i in sorted(result.calls)] == ["research", "web_search", "calculate", "made_up_tool"]


def test_openai_stream_keeps_thought_signatures_and_splits_calls_without_index():
    calls, _ = run(openai.drain_stream(stream_of([
        chunk_tool(0, "web_search", {"query": "q1"}, sig="SIG1", null_index=True),
        chunk_tool(1, "web_search", {"query": "q2"}, sig="SIG2", null_index=True)]), _ignore))
    msg = llm.assistant_tool_call_msg(calls, "")
    assert len(msg["tool_calls"]) == 2
    assert msg["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "SIG1"


def test_rejected_sampling_params_are_dropped_and_remembered():
    config.LLM_TEMPERATURE, config.LLM_TOP_P = 0.9, 0.95
    seen, create = _recording(bad_request(BadRequestError, "Unsupported value: 'temperature' does not support 0.9"))
    adapter = OpenAIAdapter(client=ns(chat=ns(completions=ns(create=create))))
    assert run(adapter._create_stream(HIST, tools=None)) == "S"
    assert seen[0]["temperature"] == 0.9 and seen[0]["top_p"] == 0.95
    assert "temperature" not in seen[1] and "top_p" not in seen[1]
    assert adapter.sampling_supported is False


def test_structured_param_field_wins_over_message_wording():
    config.LLM_TEMPERATURE = 0.7
    seen, create = _recording(bad_request(BadRequestError, "bad request",
                                          {"error": {"param": "temperature", "message": "bad request"}}))
    adapter = OpenAIAdapter(client=ns(chat=ns(completions=ns(create=create))))
    assert run(adapter._create_stream(HIST, tools=None)) == "S"
    assert "temperature" not in seen[1] and adapter.sampling_supported is False


def test_tool_pairing_error_is_a_business_error_not_a_tools_rejection():
    error = bad_request(BadRequestError, "unexpected `tool_use_id` found in `tool_result` blocks")
    assert rejected_param(error) is None


def test_rejected_extra_body_is_dropped_and_remembered():
    config.LLM_EXTRA_BODY = {"thinking": {"type": "enabled", "budget_tokens": 1000}}
    seen, create = _recording(bad_request(BadRequestError, "Unrecognized request argument supplied: thinking"))
    adapter = OpenAIAdapter(client=ns(chat=ns(completions=ns(create=create))))
    assert run(adapter._create_stream(HIST, tools=None)) == "S"
    assert seen[0]["extra_body"]["thinking"]["budget_tokens"] == 1000
    assert "extra_body" not in seen[1] and adapter.extra_body_supported is False


def test_rejected_stream_options_are_dropped_and_usage_read_from_last_chunk():
    seen, create = _recording(bad_request(BadRequestError, "Unrecognized request argument supplied: stream_options"))
    adapter = OpenAIAdapter(client=ns(chat=ns(completions=ns(create=create))))
    assert run(adapter._create_stream(HIST, tools=None)) == "S"
    assert seen[0]["stream_options"] == {"include_usage": True} and "stream_options" not in seen[1]
    assert adapter.usage_supported is False
    result = run(adapter._drain(stream_of([
        ns(choices=[ns(delta=ns(content="好", tool_calls=None))], usage=None),
        ns(choices=[], usage=ns(prompt_tokens=120, completion_tokens=7))]), _ignore))
    assert result.content == "好" and (result.usage.input, result.usage.output) == (120, 7)


def test_claude_history_conversion():
    """system 单独拿出、图片转 base64 块、思考块回传、连续的工具结果合并成一条 user。"""
    history = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": [{"type": "text", "text": "看图"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "再查"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "web_search", "arguments": '{"query":"a"}'},
             "extra_content": {"anthropic_thinking": [{"type": "thinking", "thinking": "推理", "signature": "SIG"}]}},
            {"id": "t2", "function": {"name": "open_url", "arguments": '{"url":"http://x"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "name": "web_search", "content": "R1"},
        {"role": "tool", "tool_call_id": "t2", "name": "open_url", "content": "R2"},
    ]
    system, msgs = claude.to_anthropic_messages(history)
    assert system == "SYS" and len(msgs) == 5
    assert msgs[0]["content"][1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                  "data": "QUJD"}}
    assert msgs[3]["content"][0] == {"type": "thinking", "thinking": "推理", "signature": "SIG"}
    assert msgs[3]["content"][1]["type"] == "tool_use" and msgs[3]["content"][1]["input"] == {"query": "a"}
    assert msgs[4]["role"] == "user" and [b["tool_use_id"] for b in msgs[4]["content"]] == ["t1", "t2"]


def test_claude_user_text_after_tool_results_joins_the_same_turn():
    history = [{"role": "user", "content": "算一下"},
               {"role": "assistant", "content": "", "tool_calls": [
                   {"id": "t1", "function": {"name": "calculate", "arguments": '{"expression":"1+1"}'}}]},
               {"role": "tool", "tool_call_id": "t1", "name": "calculate", "content": "2"},
               {"role": "user", "content": "直接作答"}]
    _, msgs = claude.to_anthropic_messages(history)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]  # 严格交替
    assert [b["type"] for b in msgs[2]["content"]] == ["tool_result", "text"]


def test_claude_stream_collects_text_tool_use_and_thinking():
    events = [
        ns(type="message_start"),
        ns(type="content_block_start", index=0, content_block=ns(type="thinking", thinking="")),
        ns(type="content_block_delta", index=0, delta=ns(type="thinking_delta", thinking="推理")),
        ns(type="content_block_delta", index=0, delta=ns(type="signature_delta", signature="SG")),
        ns(type="content_block_stop", index=0),
        ns(type="content_block_start", index=1, content_block=ns(type="text", text="")),
        ns(type="content_block_delta", index=1, delta=ns(type="text_delta", text="正文")),
        ns(type="content_block_start", index=2, content_block=ns(type="tool_use", id="tu1", name="web_search")),
        ns(type="content_block_delta", index=2, delta=ns(type="input_json_delta", partial_json='{"query":')),
        ns(type="content_block_delta", index=2, delta=ns(type="input_json_delta", partial_json='"q"}')),
        ns(type="message_stop"),
    ]
    got = []

    async def sink(text):
        got.append(text)

    calls, content = run(claude.drain_stream(stream_of(events), sink))
    assert content == "正文" and got == ["正文"]
    call = llm.assistant_tool_call_msg(calls, content)["tool_calls"][0]
    assert call["id"] == "tu1" and json.loads(call["function"]["arguments"]) == {"query": "q"}
    assert call["extra_content"]["anthropic_thinking"] == [{"type": "thinking", "thinking": "推理", "signature": "SG"}]


def test_claude_usage_counts_cached_input_and_final_output():
    events = [
        ns(type="message_start", message=ns(usage=ns(input_tokens=10, cache_read_input_tokens=900,
                                                     cache_creation_input_tokens=50, output_tokens=1))),
        ns(type="content_block_delta", index=0, delta=ns(type="text_delta", text="答")),
        ns(type="message_delta", usage=ns(output_tokens=42)),
        ns(type="message_stop"),
    ]
    result = run(ClaudeAdapter(client=ns())._drain(stream_of(events), _ignore))
    assert (result.usage.input, result.usage.output) == (960, 42)


def test_claude_lowers_max_tokens_and_drops_rejected_thinking():
    config.LLM_EXTRA_BODY = {"thinking": {"type": "enabled", "budget_tokens": 1024}}
    config.MAX_TOKENS = 200000
    seen, create = _recording(
        bad_request(anthropic.BadRequestError, "max_tokens: 200000 > 128000, which is the maximum allowed"),
        bad_request(anthropic.BadRequestError, "thinking.budget_tokens: not supported on this model"),
        result="CS")
    adapter = ClaudeAdapter(client=ns(messages=ns(create=create)))
    assert run(adapter._create_stream(HIST, tools=None)) == "CS"
    assert seen[0]["system"] == "s" and seen[0]["max_tokens"] == 200000
    assert seen[1]["max_tokens"] == 128000 and seen[1]["extra_body"]["thinking"]["budget_tokens"] == 1024
    assert "extra_body" not in seen[2] and seen[2]["max_tokens"] == 128000
    assert adapter.max_tokens_limit == 128000 and adapter.extra_body_supported is False


def _answer(*blocks):
    events = []
    for i, (btype, text) in enumerate(blocks):
        events.append(ns(type="content_block_start", index=i, content_block=ns(type=btype, thinking="", text="")))
        delta = ns(type="thinking_delta", thinking=text) if btype == "thinking" else ns(type="text_delta", text=text)
        events.append(ns(type="content_block_delta", index=i, delta=delta))
    return stream_of(events + [ns(type="message_stop")])


def test_claude_reconnects_when_the_gateway_drops_thinking():
    # 线上：cpa 不时丢掉 thinking 参数，模型不思考就把「[Question] 在吗」、英文推理之类当正文吐出来
    config.LLM_EXTRA_BODY = {"thinking": {"type": "adaptive"}}
    bad = lambda: _answer(("text", "哥 (@x, id 1) [Question] 在吗"))  # noqa: E731
    streams = [bad(), bad(), _answer(("thinking", "想想"), ("text", "在，说事"))]
    seen, create = _recording(result=None)

    async def serve(**kw):
        await create(**kw)
        return streams[len(seen) - 1]

    got = []

    async def sink(text):
        got.append(text)

    adapter = ClaudeAdapter(client=ns(messages=ns(create=serve)))
    assert run(adapter.run_round(HIST, None, sink)).content == "在，说事"
    assert got == ["在，说事"] and len(seen) == 3  # 坏的两次一个字都没发出去

    streams[:] = [bad() for _ in range(claude.THINKING_ATTEMPTS)]
    seen.clear()
    assert run(adapter.run_round(HIST, None, _ignore)).content.startswith("哥")  # 次数用完：最后一次照单全收
    assert len(seen) == claude.THINKING_ATTEMPTS

    config.LLM_EXTRA_BODY = None
    streams[:] = [bad()]
    seen.clear()
    assert run(ClaudeAdapter(client=ns(messages=ns(create=serve))).run_round(HIST, None, _ignore)).content.startswith("哥")
    assert len(seen) == 1  # 没要 thinking 就不挑


def test_gemini_stream_passes_text_and_dedupes_citations():
    a, a2, b = ns(uri="https://example.com/a", title="A"), ns(uri="https://example.com/a", title="A2"), \
        ns(uri="https://example.com/b", title="")

    def chunk(text, webs):
        meta = ns(grounding_chunks=[ns(web=w) for w in webs])
        return ns(text=text, candidates=[ns(grounding_metadata=meta)])

    got = []

    async def sink(text):
        got.append(text)

    citations, content = run(gemini.drain_stream(stream_of([chunk("Gemini ", [a]), chunk("正文", [a2, b])]), sink))
    assert content == "Gemini 正文" and got == ["Gemini ", "正文"]
    assert citations == [{"uri": "https://example.com/a", "title": "A"},
                         {"uri": "https://example.com/b", "title": "https://example.com/b"}]


_RESPONSES_HISTORY = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": [{"type": "text", "text": "看图"},
                                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "function": {"name": "calculate", "arguments": '{"expression":"1+1"}'},
         "extra_content": {"responses_reasoning": [
             {"type": "reasoning", "id": "rs_1", "encrypted_content": "ENC", "summary": []}]}}]},
    {"role": "tool", "tool_call_id": "call_1", "name": "calculate", "content": "1+1 = 2"},
]


def test_responses_history_conversion():
    instructions, items = responses.to_responses_input(_RESPONSES_HISTORY)
    assert instructions == "SYS"
    assert items[0]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,QUJD"}
    assert [i.get("type") for i in items[1:]] == ["reasoning", "function_call", "function_call_output"]
    assert items[2]["call_id"] == "call_1" and "id" not in items[2]
    assert items[3] == {"type": "function_call_output", "call_id": "call_1", "output": "1+1 = 2"}
    without = responses.to_responses_input(_RESPONSES_HISTORY, keep_reasoning=False)[1]
    assert [i.get("type") for i in without[1:]] == ["function_call", "function_call_output"]


def test_responses_stream_aggregates_calls_and_only_passes_back_encrypted_reasoning():
    call = ns(type="function_call", call_id="call_9", name="calculate", arguments="")
    events = [
        ns(type="response.output_item.done", output_index=0,
           item=ns(type="reasoning", id="rs_9", encrypted_content="E9", summary=[])),
        ns(type="response.output_item.done", output_index=1,
           item=ns(type="reasoning", id="rs_x", encrypted_content=None, summary=[])),
        ns(type="response.output_text.delta", delta="先算一下"),
        ns(type="response.output_item.added", output_index=2, item=call),
        ns(type="response.function_call_arguments.delta", output_index=2, delta='{"expression":'),
        ns(type="response.function_call_arguments.delta", output_index=2, delta='"2*3"}'),
        ns(type="response.completed"),
        ns(type="response.output_text.delta", delta="不该被读到"),
    ]
    calls, content = run(responses.drain_stream(stream_of(events), _ignore))
    assert content == "先算一下"
    tool_call = llm.assistant_tool_call_msg(calls, content)["tool_calls"][0]
    assert tool_call["id"] == "call_9" and json.loads(tool_call["function"]["arguments"]) == {"expression": "2*3"}
    # store=False 时没有加密内容的推理条目回传会报找不到
    assert [r["id"] for r in tool_call["extra_content"]["responses_reasoning"]] == ["rs_9"]


def test_responses_failed_event_raises():
    failed = ns(type="response.failed", response=ns(error=ns(code="server_error", message="boom")))
    try:
        run(responses.drain_stream(stream_of([failed]), _ignore))
    except RuntimeError as e:
        assert "boom" in str(e)
    else:
        raise AssertionError("failed event must raise")


def test_responses_drops_store_include_and_sampling_when_rejected():
    config.LLM_TEMPERATURE = 0.7
    seen, create = _recording(
        bad_request(BadRequestError, "Unsupported parameter: 'temperature' is not supported with this model.",
                    {"error": {"param": "temperature", "message": "unsupported"}}),
        bad_request(BadRequestError, "Unknown parameter: 'store'.",
                    {"error": {"param": "store", "message": "Unknown parameter: 'store'."}}),
        result="RS")
    adapter = responses.ResponsesAdapter(client=ns(responses=ns(create=create)))
    assert run(adapter._create_stream(_RESPONSES_HISTORY, tools=tools.schemas())) == "RS"
    assert seen[0]["temperature"] == 0.7 and seen[0]["store"] is False
    assert "temperature" not in seen[1] and seen[1]["include"] == ["reasoning.encrypted_content"]
    assert "store" not in seen[2] and "include" not in seen[2]
    assert all(i.get("type") != "reasoning" for i in seen[2]["input"])  # 不再回传推理条目
    assert seen[2]["instructions"] == "SYS" and seen[2]["tools"][0]["name"] == "calculate"
    assert adapter.stateless_supported is False and adapter.sampling_supported is False
