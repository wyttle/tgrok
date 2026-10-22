"""测试用的假 omp：按 --model 的值决定行为，说和 omp --mode rpc 一样的协议（stdin 一行一个命令，stdout 一行一个帧）。

  ok-*     每个任务：搜索一次、读一个网页，再交回一份报告（内容带上模型名、任务和这是这个进程的第几个任务）
  fail*    启动时 stderr 报错并以 1 退出（等不到 ready）
  blocked  接口返回网关的 HTML 错误页（stopReason=error，prompt_result 带错误）
  slow     收到任务后一直不结束，每 0.1 秒往 HEARTBEAT 文件追加一个字节（用来确认进程被结束了）
  dawdle   一直在搜，每 0.1 秒一步，收到 steer（让它收尾）才用已有资料交报告

每启动一个进程，往 STARTS 文件（有的话）追加一行模型名，用来数冷启动了几次。
"""

import json
import os
import queue
import sys
import threading
import time

args = sys.argv[1:]
model = args[args.index("--model") + 1]
assert args[args.index("--mode") + 1] == "rpc"

if os.environ.get("STARTS"):
    with open(os.environ["STARTS"], "a") as f:
        f.write(model + "\n")
if model.startswith("fail"):
    print("boom: no credentials", file=sys.stderr)
    sys.exit(1)


def emit(frame: dict) -> None:
    print(json.dumps(frame, ensure_ascii=False), flush=True)


emit({"type": "ready", "protocolVersion": 1})
commands: "queue.Queue[dict]" = queue.Queue()


def _read_stdin() -> None:
    for raw in sys.stdin:
        commands.put(json.loads(raw))
    commands.put({"type": "eof"})


threading.Thread(target=_read_stdin, daemon=True).start()
tasks = 0
while True:
    command = commands.get()
    if command["type"] == "eof":
        break
    emit({"id": command.get("id"), "type": "response", "command": command["type"], "success": True})
    if command["type"] != "prompt":
        continue
    tasks += 1
    task, rid = command["message"], command["id"]
    emit({"type": "agent_start"})
    if model == "dawdle":
        steps = 0
        while True:
            try:
                steer = commands.get_nowait()
            except queue.Empty:
                steps += 1
                emit({"type": "tool_execution_start", "toolName": "web_search", "args": {"query": f"{task} {steps}"}})
                time.sleep(0.1)
                continue
            emit({"id": steer.get("id"), "type": "response", "command": steer["type"], "success": True})
            if steer["type"] == "steer":
                break
        emit({"type": "message_end", "message": {
            "role": "assistant", "content": [{"type": "text", "text": f"搜了 {steps} 次，先交已经查到的"}],
            "stopReason": "stop", "usage": {"input": 7, "output": 3}}})
        emit({"type": "prompt_result", "id": rid, "status": "completed"})
        continue
    if model == "slow":
        while True:
            with open(os.environ["HEARTBEAT"], "a") as f:
                f.write(".")
            time.sleep(0.1)
    if model == "blocked":
        error = "403 <!doctype html><html><head><title>网站防火墙</title></head><body>拦截</body></html>"
        emit({"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": "error",
                                                 "errorMessage": error}})
        emit({"type": "prompt_result", "id": rid, "status": "error", "error": {"message": error}})
        continue
    emit({"type": "tool_execution_start", "toolName": "web_search", "args": {"query": task}})
    emit({"type": "tool_execution_end", "toolName": "web_search", "result": {}})
    emit({"type": "tool_execution_start", "toolName": "read", "args": {"path": "https://www.example.com/a/"}})
    emit({"type": "tool_execution_end", "toolName": "read", "result": {"content": "x" * 200_000}})
    print("not json", flush=True)
    emit({"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": f"{model} 的报告：{task}（第 {tasks} 个任务）"}],
        "stopReason": "stop", "usage": {"input": 10, "output": 5}}})
    emit({"type": "agent_end"})
    emit({"type": "prompt_result", "id": rid, "status": "completed"})
