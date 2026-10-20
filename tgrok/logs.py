"""日志：统一格式，输出到控制台和按天滚动的文件。

每个要回复的请求分一个编号（#12），请求里（包括它起的工具和调研子任务）打的日志行都带上，
几个请求同时进行时也能按编号分开看；请求结束时 handlers 打一行汇总（耗时、轮次、工具、token）。
本模块不导入 config：入口在导入 tgrok 其余部分之前就要配好控制台日志。
"""

import contextvars
import itertools
import logging
import time
from dataclasses import dataclass, field
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(rid)s%(short)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

# 第三方库的逐请求 INFO（每次 HTTP 请求一行、Gemini SDK 的 AFC 提示）没有诊断价值，只留告警
_QUIET = {"httpx": logging.WARNING, "httpx2": logging.WARNING, "httpcore": logging.WARNING,
          "google_genai": logging.WARNING, "trafilatura": logging.ERROR}


@dataclass
class Request:
    """一个请求的累计：模型轮次、调用过的工具、所有模型调用（含调研、搜索）的 token。"""
    rid: int
    t0: float = field(default_factory=time.monotonic)
    rounds: int = 0
    tools: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.t0


_current: contextvars.ContextVar[Request | None] = contextvars.ContextVar("tgrok_request", default=None)
_ids = itertools.count(1)


def begin() -> Request:
    """开始一个请求：之后在这个任务（和它创建的子任务）里打的日志都带上编号。"""
    req = Request(next(_ids))
    _current.set(req)
    return req


def current() -> Request | None:
    return _current.get()


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        req = _current.get()
        record.rid = f"#{req.rid} " if req is not None else ""
        record.short = record.name.removeprefix("tgrok.")
        return super().format(record)


def setup_console() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_Formatter(FORMAT, DATEFMT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for name, level in _QUIET.items():
        logging.getLogger(name).setLevel(level)


def add_file(directory: Path, keep_days: int) -> Path | None:
    """再写一份到 directory/bot.log：每天零点换文件（旧的加日期后缀），留 keep_days 天。0 = 不写文件。"""
    if keep_days <= 0:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "bot.log"
    handler = TimedRotatingFileHandler(path, when="midnight", backupCount=keep_days, encoding="utf-8")
    handler.setFormatter(_Formatter(FORMAT, DATEFMT))
    logging.getLogger().addHandler(handler)
    return path


def one_line(text: str, limit: int = 80) -> str:
    """日志里引用用户文字：压成一行并截断，多行提问不会把日志拆散。"""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)
