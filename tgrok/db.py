"""bot 的 SQLite 数据库（MEMORY_DB）：长期记忆、追问用的对话历史、最近群聊和用量统计都在这一个文件里，
重启后接着用。

所有读写都很小，直接在事件循环线程里同步执行。各模块用 schema() 登记自己的建表语句，第一次连接时执行；
连接之后才登记的（导入顺序靠后的模块）登记时立即执行。
"""

import sqlite3
from collections.abc import Callable

from . import config

Setup = Callable[[sqlite3.Connection], None]

_conn: sqlite3.Connection | None = None
_setups: list[Setup] = []


def schema(setup: Setup | str) -> None:
    """登记建表：SQL 脚本，或者接收连接的函数（需要按旧库结构补列时用）。"""
    if isinstance(setup, str):
        script = setup
        setup = lambda conn: conn.executescript(script)  # noqa: E731
    _setups.append(setup)
    if _conn is not None:
        setup(_conn)


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.MEMORY_DB, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        for setup in _setups:
            setup(conn)
        _conn = conn
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
