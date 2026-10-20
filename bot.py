"""入口：python bot.py（实现见 tgrok/ 包，架构文档见 docs/ARCHITECTURE.zh.md）。"""

import logging

# 先配好日志再导入 tgrok：配置解析时的告警也要按统一格式输出
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("trafilatura").setLevel(logging.ERROR)  # "discarding data" 等内部告警无诊断价值

from tgrok.tg import main  # noqa: E402

if __name__ == "__main__":
    main()
