"""测试用的假 omp：按 --model 的值决定行为，输出和 omp -p --mode json 同样格式的事件。

  ok-*    交回一份报告（内容带上模型名和任务）
  fail*   stderr 报错并以 1 退出
  slow    一直不结束，每 0.1 秒往 HEARTBEAT 文件追加一个字节（用来确认进程被结束了）
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
if model == "slow":
    while True:
        with open(os.environ["HEARTBEAT"], "a") as f:
            f.write(".")
        time.sleep(0.1)

print(json.dumps({"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": task}]}}))
print("not json")
print(json.dumps({"type": "message_end", "message": {
    "role": "assistant", "content": [{"type": "text", "text": f"{model} 的报告：{task}"}],
    "usage": {"input": 10, "output": 5}}}))
