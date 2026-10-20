"""记忆的 SQLite 表：原始记录（全文索引）、精选条目、每个聊天的整理进度。

中文检索用 FTS5 的 trigram 分词：三个字以上的片段走全文索引，更短的词退回 LIKE（只在一个聊天、保留期内的记录里扫）。
"""

import re
import sqlite3
import time
from dataclasses import dataclass

from ..db import db, schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    user_id INTEGER,          -- 说话人；bot 的回复记在提问者名下，/forgetme 时一起删
    who TEXT NOT NULL,        -- 说话人标识（名字（@用户名，id））；bot 的回复为空串
    kind TEXT NOT NULL,       -- ask = 对 bot 的提问，reply = bot 的回答，chat = 旁听到的群聊
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat_ts ON messages(chat_id, ts);
CREATE INDEX IF NOT EXISTS messages_user ON messages(user_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    user_id INTEGER,          -- NULL = 群笔记，否则是这个人在这个聊天的档案
    who TEXT NOT NULL DEFAULT '',  -- 档案主人最近一次的说话人标识
    text TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    expires REAL,             -- NULL = 长期有效
    source TEXT NOT NULL      -- tool / consolidate / migrated
);
CREATE INDEX IF NOT EXISTS notes_chat ON notes(chat_id, user_id);
CREATE TABLE IF NOT EXISTS progress (
    chat_id INTEGER PRIMARY KEY,
    done_id INTEGER NOT NULL DEFAULT 0,  -- 已整理到哪条原始记录
    tried REAL NOT NULL DEFAULT 0        -- 上次尝试整理的时间
);
"""


def _setup(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    # 旧库没有 msg_id 列：补上。同一条 Telegram 消息（比如既旁听到、又被人引用）只记一次
    if "msg_id" not in {r[1] for r in conn.execute("PRAGMA table_info(messages)")}:
        conn.execute("ALTER TABLE messages ADD COLUMN msg_id INTEGER")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_msg ON messages(chat_id, msg_id) "
                 "WHERE msg_id IS NOT NULL")


schema(_setup)


# ---- 原始记录 ----

@dataclass
class Message:
    id: int
    chat_id: int
    ts: float
    user_id: int | None
    who: str
    kind: str
    text: str
    msg_id: int | None = None


def _messages(rows) -> list[Message]:
    return [Message(**dict(r)) for r in rows]


def add_message(chat_id: int, user_id: int | None, who: str, kind: str, text: str, ts: float | None = None,
                msg_id: int | None = None) -> None:
    """msg_id 是 Telegram 的消息 id：给了就按（聊天, 消息 id）去重。"""
    db().execute("INSERT OR IGNORE INTO messages(chat_id, ts, user_id, who, kind, text, msg_id) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (chat_id, time.time() if ts is None else ts, user_id, who, kind, text, msg_id))


_TERM_RE = re.compile(r"\S+")


def search_messages(chat_id: int, query: str = "", user_id: int | None = None, since: float = 0,
                    kinds: tuple[str, ...] = ("ask", "reply", "chat"), limit: int = 20) -> list[Message]:
    """在一个聊天的原始记录里找：query 的每个词都要出现（三字以上走全文索引），可按人、时间、类型过滤。
    返回最新的 limit 条，按时间正序。"""
    where = ["m.chat_id = ?", "m.ts >= ?", f"m.kind IN ({','.join('?' * len(kinds))})"]
    params: list = [chat_id, since, *kinds]
    long_terms = []
    for term in _TERM_RE.findall(query):
        if len(term) >= 3:
            long_terms.append('"' + term.replace('"', '""') + '"')
        else:
            where.append("m.text LIKE ? ESCAPE '\\'")
            params.append("%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if long_terms:
        where.append("m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
        params.append(" AND ".join(long_terms))
    if user_id is not None:
        where.append("m.user_id = ?")
        params.append(user_id)
    rows = db().execute(f"SELECT m.* FROM messages m WHERE {' AND '.join(where)} ORDER BY m.ts DESC LIMIT ?",
                        (*params, limit)).fetchall()
    return list(reversed(_messages(rows)))


def messages_after(chat_id: int, after_id: int, limit: int) -> list[Message]:
    rows = db().execute("SELECT * FROM messages WHERE chat_id = ? AND id > ? ORDER BY id LIMIT ?",
                        (chat_id, after_id, limit)).fetchall()
    return _messages(rows)


def count_after(chat_id: int, after_id: int) -> tuple[int, float | None]:
    """(新记录条数, 其中最早一条的时间)。"""
    row = db().execute("SELECT COUNT(*), MIN(ts) FROM messages WHERE chat_id = ? AND id > ?",
                       (chat_id, after_id)).fetchone()
    return row[0], row[1]


def chats_with_messages() -> list[int]:
    return [r[0] for r in db().execute("SELECT DISTINCT chat_id FROM messages")]


def purge_messages(before: float) -> int:
    return db().execute("DELETE FROM messages WHERE ts < ?", (before,)).rowcount


def count_messages(chat_id: int) -> int:
    return db().execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()[0]


def latest_who(chat_id: int, user_id: int) -> str:
    """这个人在这个聊天里最近一次的说话人标识；没说过话返回空串。"""
    row = db().execute("SELECT who FROM messages WHERE chat_id = ? AND user_id = ? AND who != '' "
                       "ORDER BY ts DESC LIMIT 1", (chat_id, user_id)).fetchone()
    return row[0] if row else ""


# ---- 精选条目 ----

@dataclass
class Note:
    id: int
    chat_id: int
    user_id: int | None
    who: str
    text: str
    created: float
    updated: float
    expires: float | None
    source: str


def notes(chat_id: int, now: float | None = None) -> list[Note]:
    """一个聊天里没过期的全部条目（群笔记和所有人的档案），按创建顺序。"""
    now = time.time() if now is None else now
    rows = db().execute("SELECT * FROM notes WHERE chat_id = ? AND (expires IS NULL OR expires > ?) ORDER BY id",
                        (chat_id, now)).fetchall()
    return [Note(**dict(r)) for r in rows]


def get_note(note_id: int) -> Note | None:
    row = db().execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return Note(**dict(row)) if row else None


def insert_note(chat_id: int, user_id: int | None, who: str, text: str, expires: float | None, source: str,
                now: float) -> int:
    return db().execute(
        "INSERT INTO notes(chat_id, user_id, who, text, created, updated, expires, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, user_id, who, text, now, now, expires, source)).lastrowid


def update_note(note_id: int, text: str, expires: float | None, now: float) -> None:
    db().execute("UPDATE notes SET text = ?, expires = ?, updated = ? WHERE id = ?", (text, expires, now, note_id))


def delete_note(note_id: int) -> None:
    db().execute("DELETE FROM notes WHERE id = ?", (note_id,))


def purge_expired_notes(now: float) -> int:
    return db().execute("DELETE FROM notes WHERE expires IS NOT NULL AND expires <= ?", (now,)).rowcount


# ---- 整理进度 ----

def progress(chat_id: int) -> tuple[int, float]:
    row = db().execute("SELECT done_id, tried FROM progress WHERE chat_id = ?", (chat_id,)).fetchone()
    return (row[0], row[1]) if row else (0, 0.0)


def set_progress(chat_id: int, done_id: int | None = None, tried: float | None = None) -> None:
    old_done, old_tried = progress(chat_id)
    db().execute("INSERT OR REPLACE INTO progress(chat_id, done_id, tried) VALUES (?, ?, ?)",
                 (chat_id, old_done if done_id is None else done_id, old_tried if tried is None else tried))


# ---- 清除 ----

def forget_chat(chat_id: int) -> bool:
    conn = db()
    removed = conn.execute("DELETE FROM notes WHERE chat_id = ?", (chat_id,)).rowcount
    removed += conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,)).rowcount
    conn.execute("DELETE FROM progress WHERE chat_id = ?", (chat_id,))
    return removed > 0


def forget_user(user_id: int) -> int:
    """删掉一个人在所有聊天里的档案和原始记录（含 bot 回答他的那些）。"""
    conn = db()
    return (conn.execute("DELETE FROM notes WHERE user_id = ?", (user_id,)).rowcount
            + conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,)).rowcount)
