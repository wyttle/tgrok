"""入口：python bot.py（实现见 tgrok/ 包，架构文档见 docs/ARCHITECTURE.zh.md）。"""

from tgrok import logs

# 先配好控制台日志再导入 tgrok 其余部分：配置解析时的告警也要按统一格式输出
logs.setup_console()

from tgrok.app import main  # noqa: E402

if __name__ == "__main__":
    main()
