"""应用入口：组装 python-telegram-bot 应用、注册处理函数和命令菜单、打印启动配置。"""

import logging
import os
import shlex
import shutil

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler

from . import config, logs, memory
from .chat import on_cancel_button
from .commands import COMMANDS
from .handlers import MESSAGE_FILTER, handle_message
from .i18n import t

logger = logging.getLogger(__name__)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("处理更新时发生未捕获异常", exc_info=context.error)


async def post_init(app: Application) -> None:
    """启动记忆的后台维护，并向 Telegram 注册命令菜单：所有人可见基础命令，管理员私聊另外可见管理命令。"""
    memory.start_maintenance()
    base = [BotCommand(c.names[0], t(c.menu)) for c in COMMANDS if not c.admin_menu_only]
    admin = base + [BotCommand(c.names[0], t(c.menu)) for c in COMMANDS if c.admin_menu_only]
    await app.bot.set_my_commands(base)
    for admin_id in config.ADMIN_USER_IDS:
        try:
            await app.bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramError as e:
            # 管理员还没和 bot 私聊过时会 chat not found，对方先发个 /start 后重启即可
            logger.warning("为管理员 %s 注册命令菜单失败：%s", admin_id, e)


def build_application() -> Application:
    # 并发处理 update：生成回复期间取消按钮的回调才不会被堵住
    app = Application.builder().token(config.BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()
    app.add_error_handler(on_error)
    for command in COMMANDS:
        app.add_handler(CommandHandler(list(command.names), command))
    app.add_handler(CallbackQueryHandler(on_cancel_button, pattern=r"^c:\d+$"))
    app.add_handler(MessageHandler(MESSAGE_FILTER, handle_message))
    return app


def _describe(ep: config.LLMEndpoint) -> str:
    default_url = {"claude": "https://api.anthropic.com",
                   "gemini": "https://generativelanguage.googleapis.com"}.get(ep.protocol, "")
    return f"协议: {ep.protocol}, 接口: {ep.base_url or default_url}, 模型: {ep.model}"


def _log_startup() -> None:
    logger.info("Bot 启动中… %s", _describe(config.primary_endpoint()))
    fallback = config.fallback_endpoint()
    if fallback is not None:
        logger.info("备用模型已配置（主模型出错时切换）：%s", _describe(fallback))
    if config.LLM_PROTOCOL == "gemini":
        logger.info("Gemini 原生搜索模式：google_search + url_context 由 Google 服务端执行")
    elif config.GEMINI_SEARCH_MODEL:
        logger.info("混合搜索模式：web_search 由 %s + google_search grounding 执行（失败回退自带搜索源）",
                    config.GEMINI_SEARCH_MODEL)
    if config.GEMINI_HELPER_MODEL:
        logger.info("Gemini 帮手模型 %s：记忆整理", config.GEMINI_HELPER_MODEL)
    if config.GEMINI_MEDIA_MODEL and config.MEDIA_PREFETCH:
        logger.info("媒体预读已开启：视频语音一出现就由 %s 在后台转述", config.GEMINI_MEDIA_MODEL)
    if config.SEARCH_ENABLED:
        logger.info("联网搜索已开启：provider=%s（web_search + open_url）", ",".join(config.ACTIVE_PROVIDERS))
    skipped = [p for p in config.SEARCH_PROVIDERS if p not in config.ACTIVE_PROVIDERS]
    if skipped:
        logger.warning("搜索源 %s 配置不完整或名称不识别（tavily 需 TAVILY_API_KEY，searxng 需 SEARXNG_BASE_URL），已跳过",
                       ",".join(skipped))
    if config.RESEARCH_MODELS:
        logger.info("深入调研已开启：研究员 %s（omp，单个最多 %d 秒）%s",
                    ",".join(config.RESEARCH_MODELS), config.RESEARCH_TIMEOUT,
                    f"，失败时改用 {','.join(config.RESEARCH_FALLBACK_MODELS)}" if config.RESEARCH_FALLBACK_MODELS else "")
        omp = shlex.split(config.OMP_BIN, posix=os.name != "nt")[:1]
        if not omp or shutil.which(omp[0]) is None:
            logger.warning("找不到 omp 可执行文件（OMP_BIN=%s），深入调研会一直失败", config.OMP_BIN)


def main() -> None:
    log_file = logs.add_file(config.LOG_DIR, config.LOG_KEEP_DAYS)
    app = build_application()
    _log_startup()
    if log_file is not None:
        logger.info("日志同时写入 %s（每天一个文件，留 %d 天）", log_file, config.LOG_KEEP_DAYS)
    # 只订阅用到的更新类型：不收 edited_message，群友编辑一条 @bot 的旧消息不会触发重复回答
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY], drop_pending_updates=True)
