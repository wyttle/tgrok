#!/usr/bin/env python3
"""tgrok 冒烟/回归测试：python tests/smoke_test.py（无需网络与真实 Telegram）。"""
import asyncio
import json
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("SEARCH_PROVIDER", "")
os.environ.setdefault("ADMIN_USER_IDS", "999")

from tgrok import chat, config, llm, prompt, tg, web  # noqa: E402

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
        fs=FakeSent(s.log); fs.text=text; s.sent.append(fs); s.log.append(text); return fs

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

def run(coro):
    return asyncio.run(coro)

# 1. 零输出掐流 → 重试一次成功
n={"v":0}
async def cs(h, use_tools):
    n["v"]+=1
    if n["v"]==1: raise RuntimeError("dead")
    return stream_of([chunk_text("答案A")])
llm.create_stream=cs
_, ans = run(chat.stream_reply(FakeMsg(), HIST))
assert ans=="答案A" and n["v"]==2
ok("零输出重试")

# 2. 正文到手后掐流 → 按完成处理
async def cs2(h, use_tools): return stream_of([chunk_text("完整回答")], tail="cut")
llm.create_stream=cs2
_, ans = run(chat.stream_reply(FakeMsg(), HIST))
assert ans=="完整回答"
ok("掐流按完成")

# 3. 429 → 不重试 + 配额文案
n["v"]=0
async def cs3(h, use_tools):
    n["v"]+=1
    raise RuntimeError("429 RESOURCE_EXHAUSTED")
llm.create_stream=cs3
m=FakeMsg(); r=run(chat.stream_reply(m, HIST))
assert r==(None,"") and n["v"]==1 and "配额超限" in m.sent[0].text
ok("429 无重试")

# 4. 空闲看门狗收尾
config.STREAM_IDLE_TIMEOUT = 1.0
async def cs4(h, use_tools): return stream_of([chunk_text("答案B")], tail="hang")
llm.create_stream=cs4
_, ans = run(chat.stream_reply(FakeMsg(), HIST))
assert ans=="答案B"
config.STREAM_IDLE_TIMEOUT = 0
ok("空闲看门狗")

# 5. 取消：无输出 / 有部分正文 / 权限
async def cancel_case(parts, uid_cancel, expect_denied=False):
    async def csx(h, use_tools): return stream_of(parts, tail="hang")
    llm.create_stream=csx
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

# 6. TUI：思考→搜索→✓→阶段替换→正文（含 grounding 合并显示）
config.SEARCH_ENABLED = True
rounds={"n":0}
async def cs6(h, use_tools):
    rounds["n"]+=1
    if rounds["n"]==1:
        return stream_of([chunk_tool(0,"web_search",{"query":"世界杯"})])
    return stream_of([chunk_text("最终答案")])
llm.create_stream=cs6
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
llm.create_stream=cs7
m=FakeMsg(); _, ans = run(chat.stream_reply(m, HIST))
assert all(len(x) < 4096 for x in m.log)
assert ans.replace("\n","").replace(" ","")==big.replace("\n","").replace(" ","")
ok("长输出分段")

# 8. thought_signature 回传 + index=None 分槽
async def noop(d): pass
calls,_ = run(llm._drain_stream(stream_of([
    chunk_tool(0,"web_search",{"query":"q1"},sig="SIG1",null_index=True),
    chunk_tool(1,"web_search",{"query":"q2"},sig="SIG2",null_index=True)]), noop))
am = llm._assistant_tool_call_msg(calls, "")
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
c = prompt.with_time("你好")
assert c.startswith("你好") and "当前真实时间" in c
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

print(f"\nall {PASS} checks passed")
