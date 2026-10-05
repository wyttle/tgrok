#!/usr/bin/env python3
"""tgrok 冒烟/回归测试：python tests/smoke_test.py（无需网络与真实 Telegram）。"""
import asyncio
import json
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["LLM_PROTOCOL"] = "openai"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("SEARCH_PROVIDER", "")
os.environ.setdefault("ADMIN_USER_IDS", "999")

from tgrok import chat, config, llm, prompt, tg, web  # noqa: E402
from tgrok.llm import claude, gemini, openai  # noqa: E402
from tgrok.llm.claude import ClaudeAdapter  # noqa: E402
from tgrok.llm.openai import OpenAIAdapter  # noqa: E402

PASS = 0

def ok(name):
    global PASS
    PASS += 1
    print(f"  ok {PASS:2d}  {name}")

class FakeSent:
    def __init__(s, log): s.text=""; s.message_id=1; s.log=log
    async def edit_text(s, text, parse_mode=None, reply_markup=None): s.text=text; s.log.append(text)

class FakeMsg:
    def __init__(s, uid=7):
        s.sent=[]; s.log=[]; s.chat_id=-100
        s.from_user=types.SimpleNamespace(id=uid)
    async def reply_text(s, text, parse_mode=None, reply_markup=None):
        fs=FakeSent(s.log); fs.text=text; fs.message_id=len(s.sent)+1; s.sent.append(fs); s.log.append(text); return fs

def chunk_text(c):
    d=types.SimpleNamespace(content=c, tool_calls=None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=d)])

def chunk_tool(idx, name, args, sig=None, null_index=False):
    tc=types.SimpleNamespace(
        index=None if null_index else idx, id=f"c{idx}",
        function=types.SimpleNamespace(name=name, arguments=json.dumps(args)))
    tc.model_extra={"extra_content": {"google": {"thought_signature": sig}}} if sig else {}
    d=types.SimpleNamespace(content=None, tool_calls=[tc])
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=d)])

def stream_of(parts, tail="stop"):
    class S:
        def __init__(s): s.p=list(parts)
        def __aiter__(s): return s
        async def __anext__(s):
            if s.p: return s.p.pop(0)
            if tail == "stop": raise StopAsyncIteration
            if tail == "hang": await asyncio.sleep(999)
            raise RuntimeError(tail)
        async def close(s): pass
    return S()

HIST=[{"role":"system","content":"s"},{"role":"user","content":"u"}]
config.STREAM_IDLE_TIMEOUT = 0
config.GEMINI_NATIVE_SEARCH = False
orig_adapter = llm.adapter

class FakeCompletions:
    def __init__(s, create):
        async def wrapped(**kw):
            return await create(kw["messages"], "tools" in kw)
        s.create = wrapped

def set_create(create):
    llm.adapter = OpenAIAdapter(client=types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=FakeCompletions(create))))

def run(coro):
    return asyncio.run(coro)

# 1. 零输出掐流 → 重试一次成功
n={"v":0}
async def cs(h, use_tools):
    n["v"]+=1
    if n["v"]==1: raise RuntimeError("dead")
    return stream_of([chunk_text("答案A")])
set_create(cs)
_, ans = run(chat.stream_reply(FakeMsg(), HIST))
assert ans=="答案A" and n["v"]==2
ok("零输出重试")

# 2. 正文到手后掐流 → 按完成处理
async def cs2(h, use_tools): return stream_of([chunk_text("完整回答")], tail="cut")
set_create(cs2)
_, ans = run(chat.stream_reply(FakeMsg(), HIST))
assert ans=="完整回答"
ok("掐流按完成")

# 3. 429 → 不重试 + 配额文案
n["v"]=0
async def cs3(h, use_tools):
    n["v"]+=1
    raise RuntimeError("429 RESOURCE_EXHAUSTED")
set_create(cs3)
m=FakeMsg(); r=run(chat.stream_reply(m, HIST))
assert r==([],"") and n["v"]==1 and "配额超限" in m.sent[0].text
ok("429 无重试")

# 4. 空闲看门狗收尾
config.STREAM_IDLE_TIMEOUT = 1.0
async def cs4(h, use_tools): return stream_of([chunk_text("答案B")], tail="hang")
set_create(cs4)
_, ans = run(chat.stream_reply(FakeMsg(), HIST))
assert ans=="答案B"
config.STREAM_IDLE_TIMEOUT = 0
ok("空闲看门狗")

# 5. 取消：无输出 / 有部分正文 / 权限
async def cancel_case(parts, uid_cancel, expect_denied=False):
    async def csx(h, use_tools): return stream_of(parts, tail="hang")
    set_create(csx)
    m=FakeMsg(uid=7)
    tsk=asyncio.create_task(chat.stream_reply(m, HIST))
    await asyncio.sleep(0.3)
    gid=max(chat.active_generations)
    q=types.SimpleNamespace(data=f"c:{gid}", from_user=types.SimpleNamespace(id=uid_cancel), answers=[])
    async def answer(text=None, show_alert=False): q.answers.append(text)
    q.answer=answer
    await chat.on_cancel_button(types.SimpleNamespace(callback_query=q), None)
    if expect_denied:
        assert q.answers==[chat.t("cancel_denied")]
        chat.active_generations[gid][0].cancel()
    await tsk
    return m
m = run(cancel_case([], 7)); assert m.sent[0].text==chat.t("cancelled")
ok("取消·无输出")
m = run(cancel_case([chunk_text("部分")], 7)); assert "部分" in m.sent[0].text and "已取消" in m.sent[0].text
ok("取消·保留部分正文")
run(cancel_case([], 8, expect_denied=True))
ok("取消·权限拒绝")

# 6. TUI：思考->搜索->完成->阶段替换->正文（含 grounding 合并显示）
config.SEARCH_ENABLED = True
rounds={"n":0}
async def cs6(h, use_tools):
    rounds["n"]+=1
    if rounds["n"]==1:
        return stream_of([chunk_tool(0,"web_search",{"query":"世界杯"})])
    return stream_of([chunk_text("最终答案")])
set_create(cs6)
async def fake_search(q): return "[1] a\nu\ns\n\n[2] b\nu\ns"
web.run_web_search=fake_search
m=FakeMsg(); _, ans = run(chat.stream_reply(m, HIST))
zh=chat.STRINGS["zh"]["thinking_stages"]
assert ans=="最终答案" and m.log[0]==zh[0]+"…"
assert any("搜索: 世界杯" in x and zh[1] in x for x in m.log)
ok("TUI 进度序列")

# 7. 巨型单 delta 分段，内容零丢失
big="\n".join("段%d %s" % (i, "内容"*40) for i in range(100))
async def cs7(h, use_tools): return stream_of([chunk_text(big)])
set_create(cs7)
m=FakeMsg(); ids, ans = run(chat.stream_reply(m, HIST))
assert all(len(x) < 4096 for x in m.log)
assert ans.replace("\n","").replace(" ","")==big.replace("\n","").replace(" ","")
# 每一段都要能被回复追问：返回全部分段消息的 id，而不只是最后一条
assert len(ids) > 1 and ids == [x.message_id for x in m.sent]
ok("长输出分段")

# 8. thought_signature 回传 + index=None 分槽
async def noop(d): pass
calls,_ = run(openai.drain_stream(stream_of([
    chunk_tool(0,"web_search",{"query":"q1"},sig="SIG1",null_index=True),
    chunk_tool(1,"web_search",{"query":"q2"},sig="SIG2",null_index=True)]), noop))
am = llm.assistant_tool_call_msg(calls, "")
assert len(am["tool_calls"])==2
assert am["tool_calls"][0]["extra_content"]["google"]["thought_signature"]=="SIG1"
ok("签名回传/空 index 分槽")

# 9. grounding 模式：同轮多搜索合并为一次
config.GEMINI_SEARCH_MODEL = "gemini-x"
gcalls=[]
async def fake_grounded(q): gcalls.append(q); return "综述"
web.run_web_search=fake_grounded
am={"tool_calls":[
    {"id":"a","function":{"name":"web_search","arguments":json.dumps({"query":"q1"})}},
    {"id":"b","function":{"name":"web_search","arguments":json.dumps({"query":"q2"})}}]}
rs = run(chat._execute_tool_calls(am))
assert len(gcalls)==1 and "q1；q2" in gcalls[0] and "已合并" in rs[1]["content"]
config.GEMINI_SEARCH_MODEL = ""
ok("同轮搜索合并")

# 10. 时间注入挂在用户消息尾部
from datetime import datetime  # noqa: E402
c = prompt.with_time("你好")
assert c.startswith("你好") and datetime.now(config.BOT_TZ).strftime("%Y-%m-%d") in c
ok("时间注入")

# 11. 相册展开与回退
def photo_msg(mid, group=None):
    p=types.SimpleNamespace(file_id=f"photo{mid}")
    return types.SimpleNamespace(message_id=mid, chat_id=-100, media_group_id=group,
                                 photo=[p], document=None)
for mid in (12, 11, 12): tg.remember_album(photo_msg(mid, "g1"))
refs = tg._image_refs(photo_msg(11, "g1"))
assert [e["file_id"] for e in refs]==["photo11","photo12"]
assert [e["file_id"] for e in tg._image_refs(photo_msg(99, "gX"))]==["photo99"]
ok("相册缓存/回退")

# 12. URL 安全与 HTML 提取
assert web._is_public_http_url("https://a.com/x") and not web._is_public_http_url("http://127.0.0.1/x")
title, text = web._html_to_text("<html><head><title>T</title></head><body><main><p>Hi</p></main></body></html>")
assert title=="T" and "Hi" in text
ok("URL 安全/HTML 提取")

# 13. SDK 内建重试必须禁用（否则 429 时静默睡 60s+，取消/自有重试全部失效）
assert OpenAIAdapter().client.max_retries == 0
assert ClaudeAdapter().client.max_retries == 0
ok("OpenAI/Claude SDK 重试已禁用")

# 14. PTB 必须真实启用并发 update（否则取消按钮回调被生成任务堵死）
app = tg.build_application()
assert app.concurrent_updates > 1
ok("PTB 并发处理已启用")

# 15. Telegram 已过期的回调确认不应冒泡为未捕获异常
async def stale_answer(text=None):
    raise chat.BadRequest("Query is too old and response timeout expired or query id is invalid")
q = types.SimpleNamespace(answer=stale_answer)
run(chat._answer_callback(q, "x"))
ok("过期回调安全忽略")

# 16. 采样参数透传；后端拒绝时去掉重试并粘性禁用
import httpx
from openai import BadRequestError
config.LLM_TEMPERATURE, config.LLM_TOP_P = 0.9, 0.95
seen=[]
class _FC:
    async def create(s, **kw):
        seen.append(kw)
        if len(seen)==1:
            raise BadRequestError(
                "Unsupported value: 'temperature' does not support 0.9 with this model.",
                response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
                body=None)
        return "S"
a16 = OpenAIAdapter(client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=_FC())))
out = run(a16._create_stream(HIST, use_tools=False))
assert seen[0]["temperature"]==0.9 and seen[0]["top_p"]==0.95
assert out=="S" and "temperature" not in seen[1] and "top_p" not in seen[1]
assert a16.sampling_supported is False
config.LLM_TEMPERATURE = config.LLM_TOP_P = None
ok("采样参数透传/拒绝降级")

# 17. 结构化 param 优先：message 无拒绝措辞也必须触发采样降级
config.LLM_TEMPERATURE = 0.7
seen=[]
class _StructuredSampling:
    async def create(s, **kw):
        seen.append(kw)
        if len(seen) == 1:
            raise BadRequestError(
                "bad request",
                response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
                body={"error": {"param": "temperature", "message": "bad request"}})
        return "S"
a_structured = OpenAIAdapter(client=types.SimpleNamespace(
    chat=types.SimpleNamespace(completions=_StructuredSampling())))
out = run(a_structured._create_stream(HIST, use_tools=False))
assert out == "S" and seen[0]["temperature"] == 0.7 and "temperature" not in seen[1]
assert a_structured.sampling_supported is False
config.LLM_TEMPERATURE = None
ok("结构化 param 采样降级")

# 18. tool_use/tool_result 配对错误是业务 400，不得分类为 tools 拒绝
business_400 = BadRequestError(
    "unexpected `tool_use_id` found in `tool_result` blocks",
    response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None)
assert llm.base.rejected_param(business_400) is None
ok("tool_use_id 业务错误不降级")

# 19. LLM_EXTRA_BODY 透传（thinking 等厂商私有参数）；后端拒绝时去掉重试并粘性禁用
config.LLM_EXTRA_BODY = {"thinking": {"type": "enabled", "budget_tokens": 1000}}
seen=[]
class _FC2:
    async def create(s, **kw):
        seen.append(kw)
        if len(seen)==1:
            raise BadRequestError(
                "Unrecognized request argument supplied: thinking",
                response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
                body=None)
        return "S"
a17 = OpenAIAdapter(client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=_FC2())))
out = run(a17._create_stream(HIST, use_tools=False))
assert seen[0]["extra_body"]["thinking"]["budget_tokens"] == 1000
assert out=="S" and "extra_body" not in seen[1]
assert a17.extra_body_supported is False
config.LLM_EXTRA_BODY = None
ok("extra_body 透传/拒绝降级")

# 18. Claude 原生：OpenAI 历史 → Anthropic messages（system/图片/思考回传/连续 tool_result 合并）
hist18 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]},
    {"role": "assistant", "content": "ok"},
    {"role": "user", "content": "再查"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "t1", "function": {"name": "web_search", "arguments": '{"query":"a"}'},
         "extra_content": {"anthropic_thinking": [
             {"type": "thinking", "thinking": "推理", "signature": "SIG"}]}},
        {"id": "t2", "function": {"name": "open_url", "arguments": '{"url":"http://x"}'}}]},
    {"role": "tool", "tool_call_id": "t1", "name": "web_search", "content": "R1"},
    {"role": "tool", "tool_call_id": "t2", "name": "open_url", "content": "R2"},
]
sys_text, msgs = claude.to_anthropic_messages(hist18)
assert sys_text == "SYS" and len(msgs) == 5
img = msgs[0]["content"][1]
assert img["type"] == "image" and img["source"] == {"type": "base64", "media_type": "image/png", "data": "QUJD"}
am18 = msgs[3]["content"]
assert am18[0] == {"type": "thinking", "thinking": "推理", "signature": "SIG"}
assert am18[1]["type"] == "tool_use" and am18[1]["input"] == {"query": "a"} and am18[1]["id"] == "t1"
assert msgs[4]["role"] == "user" and [b["tool_use_id"] for b in msgs[4]["content"]] == ["t1", "t2"]
ok("Claude 历史转换")

# 19. Claude 原生流消费：thinking/text/tool_use 事件聚合，思考块挂到首个调用回传
def ev(**kw): return types.SimpleNamespace(**kw)
events = [
    ev(type="message_start"),
    ev(type="content_block_start", index=0, content_block=ev(type="thinking", thinking="")),
    ev(type="content_block_delta", index=0, delta=ev(type="thinking_delta", thinking="推理")),
    ev(type="content_block_delta", index=0, delta=ev(type="signature_delta", signature="SG")),
    ev(type="content_block_stop", index=0),
    ev(type="content_block_start", index=1, content_block=ev(type="text", text="")),
    ev(type="content_block_delta", index=1, delta=ev(type="text_delta", text="正文")),
    ev(type="content_block_start", index=2, content_block=ev(type="tool_use", id="tu1", name="web_search")),
    ev(type="content_block_delta", index=2, delta=ev(type="input_json_delta", partial_json='{"query":')),
    ev(type="content_block_delta", index=2, delta=ev(type="input_json_delta", partial_json='"q"}')),
    ev(type="message_stop"),
]
got = []
async def sink(d): got.append(d)
calls, content = run(claude.drain_stream(stream_of(events), sink))
assert content == "正文" and got == ["正文"]
am19 = llm.assistant_tool_call_msg(calls, content)
assert am19["tool_calls"][0]["id"] == "tu1"
assert json.loads(am19["tool_calls"][0]["function"]["arguments"]) == {"query": "q"}
assert am19["tool_calls"][0]["extra_content"]["anthropic_thinking"] == [
    {"type": "thinking", "thinking": "推理", "signature": "SG"}]
ok("Claude 流消费/思考块回传")

# 20. Claude 降级链：max_tokens 超限解析上限回退 + thinking 被拒粘性禁用
import anthropic as _an
config.LLM_EXTRA_BODY = {"thinking": {"type": "enabled", "budget_tokens": 1024}}
_orig_max = claude.MAX_TOKENS
claude.MAX_TOKENS = 200000
seen = []
class _CC:
    async def create(s, **kw):
        seen.append(kw)
        if len(seen) == 1:
            raise _an.BadRequestError(
                "max_tokens: 200000 > 128000, which is the maximum allowed",
                response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None)
        if len(seen) == 2:
            raise _an.BadRequestError(
                "thinking.budget_tokens: not supported on this model",
                response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None)
        return "CS"
a20 = ClaudeAdapter(client=types.SimpleNamespace(messages=_CC()))
out = run(a20._create_stream(HIST, use_tools=False))
assert out == "CS"
assert seen[0]["system"] == "s" and seen[0]["max_tokens"] == 200000
assert seen[0]["extra_body"]["thinking"]["budget_tokens"] == 1024
assert seen[1]["max_tokens"] == 128000
assert "extra_body" not in seen[2] and seen[2]["max_tokens"] == 128000
assert a20.max_tokens_limit == 128000 and a20.extra_body_supported is False
claude.MAX_TOKENS = _orig_max
config.LLM_EXTRA_BODY = None
ok("Claude 降级链")

# 21. Gemini 主链：服务端工具协议只执行一轮，引用拼到最终正文
class _GA:
    supports_tool_loop = False
    def __init__(s): s.n = 0
    async def run_round(s, history, use_tools, on_text):
        s.n += 1
        assert use_tools is False
        await on_text("Gemini 答案")
        return llm.RoundResult(content="Gemini 答案", citations=[
            {"uri": "https://example.com/a", "title": "来源 A"}])
ga = _GA(); llm.adapter = ga
_, ans = run(chat.stream_reply(FakeMsg(), HIST))
assert ga.n == 1 and ans.endswith("来源：\n[来源 A](https://example.com/a)")
ok("Gemini chat 单轮/引用输出")

# 22. Gemini 原生流消费：正文透传，grounding 引用按 uri 去重
web_a = types.SimpleNamespace(uri="https://example.com/a", title="A")
web_a2 = types.SimpleNamespace(uri="https://example.com/a", title="A2")
web_b = types.SimpleNamespace(uri="https://example.com/b", title="")
def gchunk(text, webs):
    chunks = [types.SimpleNamespace(web=w) for w in webs]
    gm = types.SimpleNamespace(grounding_chunks=chunks)
    return types.SimpleNamespace(text=text, candidates=[types.SimpleNamespace(grounding_metadata=gm)])
got = []
async def gsink(d): got.append(d)
citations, content = run(gemini.drain_stream(stream_of([
    gchunk("Gemini ", [web_a]), gchunk("正文", [web_a2, web_b])]), gsink))
assert content == "Gemini 正文" and got == ["Gemini ", "正文"]
assert citations == [
    {"uri": "https://example.com/a", "title": "A"},
    {"uri": "https://example.com/b", "title": "https://example.com/b"},
]
ok("Gemini 流消费/引用去重")

# 27. 对话缓存按近似内容量驱逐，并在覆盖时刷新顺序与会计
_saved_conversations = tg.conversations.copy()
_saved_conv_sizes = tg._conv_sizes.copy()
_saved_conv_total = tg._conv_total
_saved_conv_budget = tg.CONVERSATION_CONTENT_BUDGET
try:
    tg.conversations.clear()
    tg._conv_sizes.clear()
    tg._conv_total = 0
    tg.CONVERSATION_CONTENT_BUDGET = 10
    tg.remember(1, 1, [{"role": "user", "content": "aaaa"}])
    tg.remember(1, 2, [{"role": "user", "content": "bbbb"}])
    tg.remember(1, 1, [{"role": "user", "content": "ccccc"}])
    assert list(tg.conversations) == [(1, 2), (1, 1)] and tg._conv_total == 9
    assert tg._conv_sizes[(1, 1)] == 5
    tg.remember(1, 3, [{"role": "user", "content": "ddd"}])
    assert list(tg.conversations) == [(1, 1), (1, 3)]
    assert tg._conv_total == sum(tg._history_chars(h) for h in tg.conversations.values())
    assert tg._conv_total == sum(tg._conv_sizes.values()) == 8
    tg.conversations.clear()
    tg._conv_sizes.clear()
    tg._conv_total = 0
    tg.remember(2, 1, [{"role": "user", "content": "x" * 20}])
    assert list(tg.conversations) == [(2, 1)] and tg._conv_total == 20
finally:
    tg.conversations.clear()
    tg.conversations.update(_saved_conversations)
    tg._conv_sizes.clear()
    tg._conv_sizes.update(_saved_conv_sizes)
    tg._conv_total = _saved_conv_total
    tg.CONVERSATION_CONTENT_BUDGET = _saved_conv_budget
ok("对话缓存内容预算/覆盖会计")

# 28. 工具轮草稿不并入终稿：请求了工具的轮次正文被丢弃；终轮空手时回用草稿兜底
rounds28 = {"n": 0}
async def cs28(h, use_tools):
    rounds28["n"] += 1
    if rounds28["n"] == 1:
        return stream_of([chunk_text("草稿正文。"), chunk_tool(0, "web_search", {"query": "q"})])
    return stream_of([chunk_text("终稿正文。")])
set_create(cs28)
async def fake_search28(q): return "[1] r\nu\ns"
web.run_web_search = fake_search28
m = FakeMsg(); _, ans = run(chat.stream_reply(m, HIST))
assert ans == "终稿正文。", ans
rounds28["n"] = 0
async def cs28b(h, use_tools):
    rounds28["n"] += 1
    if rounds28["n"] == 1:
        return stream_of([chunk_text("唯一草稿"), chunk_tool(0, "web_search", {"query": "q"})])
    return stream_of([])
set_create(cs28b)
m = FakeMsg(); _, ans = run(chat.stream_reply(m, HIST))
assert ans == "唯一草稿", ans
ok("工具轮草稿丢弃/兜底")

# 29. 中间编辑遇到 Telegram 网络错误：只丢这一次显示，不能截断正文
from telegram.error import TimedOut  # noqa: E402
class FlakySent(FakeSent):
    async def edit_text(s, text, parse_mode=None, reply_markup=None):
        if not s.log or s.log[-1] != "flaky":
            s.log.append("flaky")
            raise TimedOut()
        await FakeSent.edit_text(s, text, parse_mode, reply_markup)
class FlakyMsg(FakeMsg):
    async def reply_text(s, text, parse_mode=None, reply_markup=None):
        fs = FlakySent(s.log); fs.text = text; s.sent.append(fs); return fs
def slow_stream(parts):
    class S:
        def __init__(s): s.p = list(parts)
        def __aiter__(s): return s
        async def __anext__(s):
            await asyncio.sleep(0.05)  # 让占位消息先落地，首个 delta 才会触发中间编辑
            if s.p: return s.p.pop(0)
            raise StopAsyncIteration
        async def close(s): pass
    return S()
async def cs29(h, use_tools): return slow_stream([chunk_text("前半段，"), chunk_text("后半段。")])
set_create(cs29)
m = FlakyMsg(); _, ans = run(chat.stream_reply(m, HIST))
assert ans == "前半段，后半段。", ans
assert "flaky" in m.log and m.sent[0].text == "前半段，后半段。"
ok("中间编辑网络错误不截断正文")

# 30. 历史截断后第一条对话必须是 user（严格交替的接口/模板拒绝 assistant 开头）
long_hist = [{"role": "system", "content": "s"}] + [
    {"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(2 * tg.MAX_HISTORY + 1)]
trimmed = tg.trim_history(long_hist)
assert trimmed[0]["role"] == "system" and trimmed[1]["role"] == "user"
assert trimmed[-1] is long_hist[-1] and len(trimmed) <= tg.MAX_HISTORY + 1
ok("历史截断从 user 开始")

# 31. 配置向导读写多行值：不能截断提示词；写出的单行值 python-dotenv 读回逐字一致
import importlib.util  # noqa: E402
from dotenv import dotenv_values  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "configure", Path(__file__).resolve().parent.parent / "configure.py")
configure = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(configure)
prompt31 = '第一段\n\n# 标题\n- 带"引号"、反斜杠\\和 # 号\n结尾'
parsed31 = configure.parse_env("A=1\nSYSTEM_PROMPT='" + prompt31 + "'\nB=2\n")
assert parsed31 == {"A": "1", "SYSTEM_PROMPT": prompt31, "B": "2"}, parsed31
line31 = configure.env_line("SYSTEM_PROMPT", prompt31)
assert "\n" not in line31
assert configure.parse_env(line31 + "\n")["SYSTEM_PROMPT"] == prompt31
import io  # noqa: E402
assert dotenv_values(stream=io.StringIO(line31 + "\n"))["SYSTEM_PROMPT"] == prompt31
ok("配置向导多行值读写")

# 32. calculate：结果精确、日期推算正确；越界表达式和沙箱逃逸被拒绝而不是执行
from tgrok import calc  # noqa: E402
assert calc.evaluate("1299*0.85") == "1104.15"
assert calc.evaluate("2^10 + 3*(4-1)") == "1033"
assert calc.evaluate("7/2 >= 3.5") == "true"
assert calc.evaluate("date('2026-12-25') - date('2026-10-05')") == "81 days"
assert calc.evaluate("date('2024-02-28') + days(1)").startswith("2024-02-29")
for bad in ("9**9**9", "factorial(5000)", "__import__('os')", "(1).__class__", "'a'*10", "1/0"):
    try:
        calc.evaluate(bad)
        raise AssertionError(f"accepted {bad}")
    except calc.CalcError:
        pass
assert calc.run_calculate("1/0").startswith("（")  # 失败文案让进度行显示为失败
ok("calculate 精确计算与边界")

# 33. Responses：chat 历史转换（instructions/图片/推理回传/工具结果）与事件流聚合
from tgrok.llm import responses as resp_mod  # noqa: E402
hist33 = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "function": {"name": "calculate", "arguments": '{"expression":"1+1"}'},
         "extra_content": {"responses_reasoning": [
             {"type": "reasoning", "id": "rs_1", "encrypted_content": "ENC", "summary": []}]}}]},
    {"role": "tool", "tool_call_id": "call_1", "name": "calculate", "content": "1+1 = 2"},
]
instr33, items33 = resp_mod.to_responses_input(hist33)
assert instr33 == "SYS"
assert items33[0]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,QUJD"}
assert [i.get("type") for i in items33[1:]] == ["reasoning", "function_call", "function_call_output"]
assert items33[2]["call_id"] == "call_1" and "id" not in items33[2]
assert items33[3] == {"type": "function_call_output", "call_id": "call_1", "output": "1+1 = 2"}
assert [i.get("type") for i in resp_mod.to_responses_input(hist33, keep_reasoning=False)[1][1:]] == [
    "function_call", "function_call_output"]
def rev(**kw): return types.SimpleNamespace(**kw)
fc_item = rev(type="function_call", call_id="call_9", name="calculate", arguments="")
events33 = [
    rev(type="response.output_item.done", output_index=0,
        item=rev(type="reasoning", id="rs_9", encrypted_content="E9", summary=[])),
    rev(type="response.output_item.done", output_index=1,
        item=rev(type="reasoning", id="rs_x", encrypted_content=None, summary=[])),
    rev(type="response.output_text.delta", delta="先算一下"),
    rev(type="response.output_item.added", output_index=2, item=fc_item),
    rev(type="response.function_call_arguments.delta", output_index=2, delta='{"expression":'),
    rev(type="response.function_call_arguments.delta", output_index=2, delta='"2*3"}'),
    rev(type="response.completed"),
    rev(type="response.output_text.delta", delta="不该被读到"),
]
got33 = []
async def sink33(d): got33.append(d)
calls33, content33 = run(resp_mod.drain_stream(stream_of(events33), sink33))
assert content33 == "先算一下" and got33 == ["先算一下"]
am33 = llm.assistant_tool_call_msg(calls33, content33)
assert am33["tool_calls"][0]["id"] == "call_9"
assert json.loads(am33["tool_calls"][0]["function"]["arguments"]) == {"expression": "2*3"}
# 只回传带加密内容的推理条目：store=False 时没有加密内容的条目回传会报找不到
assert [r["id"] for r in am33["tool_calls"][0]["extra_content"]["responses_reasoning"]] == ["rs_9"]
try:
    run(resp_mod.drain_stream(stream_of([rev(type="response.failed",
        response=rev(error=rev(code="server_error", message="boom")))]), sink33))
    raise AssertionError("failed event must raise")
except RuntimeError as e:
    assert "boom" in str(e)
ok("Responses 历史转换/流聚合")

# 34. Responses 降级：不认 store/include 时去掉重试并不再回传推理条目；采样参数被拒同样降级
config.LLM_TEMPERATURE = 0.7
seen34 = []
class _RC:
    async def create(s, **kw):
        seen34.append(kw)
        if len(seen34) == 1:
            raise BadRequestError("Unsupported parameter: 'temperature' is not supported with this model.",
                response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
                body={"error": {"param": "temperature", "message": "unsupported"}})
        if len(seen34) == 2:
            raise BadRequestError("Unknown parameter: 'store'.",
                response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
                body={"error": {"param": "store", "message": "Unknown parameter: 'store'."}})
        return "RS"
a34 = resp_mod.ResponsesAdapter(client=types.SimpleNamespace(responses=_RC()))
assert run(a34._create_stream(hist33, use_tools=True)) == "RS"
assert seen34[0]["temperature"] == 0.7 and seen34[0]["store"] is False
assert "temperature" not in seen34[1] and seen34[1]["include"] == ["reasoning.encrypted_content"]
assert "store" not in seen34[2] and "include" not in seen34[2]
assert all(i.get("type") != "reasoning" for i in seen34[2]["input"])
assert seen34[2]["instructions"] == "SYS" and seen34[2]["tools"][0]["name"] == "calculate"
assert a34.stateless_supported is False and a34.sampling_supported is False
config.LLM_TEMPERATURE = None
ok("Responses 参数降级")

# 35. 端到端：Responses 适配器 + calculate 工具循环，算出的结果回灌给模型、进度显示算式
config.SEARCH_ENABLED = False
seen35 = []
class _RC35:
    async def create(s, **kw):
        seen35.append(kw)
        if len(seen35) == 1:
            return stream_of([
                rev(type="response.output_item.added", output_index=0,
                    item=rev(type="function_call", call_id="c1", name="calculate", arguments="")),
                rev(type="response.output_item.done", output_index=0,
                    item=rev(type="function_call", call_id="c1", name="calculate",
                             arguments='{"expression":"1299*0.85"}')),
                rev(type="response.completed")])
        return stream_of([rev(type="response.output_text.delta", delta="打完折 1104.15。"),
                          rev(type="response.completed")])
llm.adapter = resp_mod.ResponsesAdapter(client=types.SimpleNamespace(responses=_RC35()))
m = FakeMsg(); _, ans = run(chat.stream_reply(m, HIST))
assert ans == "打完折 1104.15。", ans
assert [t_["name"] for t_ in seen35[0]["tools"]] == ["calculate"]  # 没配搜索源也能用计算
out35 = [i for i in seen35[1]["input"] if i.get("type") == "function_call_output"]
assert out35 == [{"type": "function_call_output", "call_id": "c1", "output": "1299*0.85 = 1104.15"}]
assert any("计算: 1299*0.85" in x and "= 1104.15" in x for x in m.log)
ok("Responses + calculate 工具循环")

# 36. 配置向导的额外参数：键=值 与 JSON 解析一致、能回显；推理强度按协议写成对应字段
params36 = "thinking.type=enabled, thinking.budget_tokens=1000, verbosity=low, stream_usage=true"
want36 = {"thinking": {"type": "enabled", "budget_tokens": 1000}, "verbosity": "low", "stream_usage": True}
assert configure.parse_extra_params(params36) == want36
assert configure.parse_extra_params(json.dumps(want36)) == want36
assert configure.parse_extra_params(configure.format_extra_params(want36)) == want36
assert configure.parse_extra_params("no-equals-sign") is None
assert configure.effort_body("low", "responses") == {"reasoning": {"effort": "low"}}
assert configure.effort_body("low", "") == {"reasoning_effort": "low"}
assert configure.is_reasoning_gpt("openai/gpt-6.1-sol") and not configure.is_reasoning_gpt("gpt-4o")
ok("配置向导额外参数/推理强度")

llm.adapter = orig_adapter

print(f"\nall {PASS} checks passed")
