"""测试用的假 omp：按 --model 的值决定行为，输出和 omp -p --mode json 同样格式的事件。

  ok-*     搜索一次、读一个网页，再交回一份报告（内容带上模型名和任务）
  fail*    stderr 报错并以 1 退出
  blocked  接口返回网关的 HTML 错误页（stopReason=error），stderr 只有一行没用的日志路径
  aborted  到了 --max-time 被 omp 自己中止（stopReason=aborted）
  slow     一直不结束，每 0.1 秒往 HEARTBEAT 文件追加一个字节（用来确认进程被结束了）
"""

import json
import os
import sys
import time

args = sys.argv[1:]
model = args[args.index("--model") + 1]
task = args[args.index("--") + 1]

if model.startswith("fail"):
    print("boom: no credentials", file=sys.stderr)
    sys.exit(1)
if model in ("blocked", "aborted"):
    stop = "error" if model == "blocked" else "aborted"
    error = "403 <!doctype html><html><head><title>网站防火墙</title></head><body>拦截</body></html>"
    print(json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": stop,
                                                         "errorMessage": error if stop == "error" else None}}))
    print("  logs: /root/.omp/logs/omp.log · re-run with PI_DEBUG_STARTUP=1", file=sys.stderr)
    sys.exit(1)
if model == "slow":
    while True:
        with open(os.environ["HEARTBEAT"], "a") as f:
            f.write(".")
        time.sleep(0.1)

print(json.dumps({"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": task}]}}))
print(json.dumps({"type": "agent_start"}))
print(json.dumps({"type": "tool_execution_start", "toolName": "web_search", "args": {"query": task}}))
print(json.dumps({"type": "tool_execution_end", "toolName": "web_search", "result": {}}))
print(json.dumps({"type": "tool_execution_start", "toolName": "read", "args": {"path": "https://www.example.com/a/"}}))
print(json.dumps({"type": "tool_execution_end", "toolName": "read", "result": {"content": "x" * 200_000}}))
print("not json")
print(json.dumps({"type": "message_end", "message": {
    "role": "assistant", "content": [{"type": "text", "text": f"{model} 的报告：{task}"}],
    "usage": {"input": 10, "output": 5}}}))
