"""追问用的对话历史：bot 的每条回复消息对应一段对话历史，群友回复那条消息就能接着聊。

key = (chat_id, bot 回复消息的 message_id)，value = OpenAI 格式的 messages 列表。

两层存储：
- 内存缓存保留原样（包括图片 data URL），按条目数和近似内容字符量（文本和图片 data URL 的长度）两个上限
  驱逐最旧的；字符量不是进程内存硬上限，共享消息对象会被重复计数，高估只会让缓存更早驱逐。
- 数据库存纯文本版本（不存系统提示词，图片换成「[图片]」），保留 CONVERSATION_KEEP_DAYS 天。
  内存里没有（重启过或已被驱逐）时从数据库取，系统提示词换成当前的。
"""

import json
import time
from collections import OrderedDict

from . import config, prompt
from .db import db, schema
from .i18n import t

schema("""
CREATE TABLE IF NOT EXISTS conversations (
    chat_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,   -- bot 回复消息的 id
    user_id INTEGER,           -- 提问者，/forgetme 时一起删
    ts REAL NOT NULL,
    messages TEXT NOT NULL,    -- 去掉系统提示词和图片后的 messages（JSON）
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS conversations_ts ON conversations(ts);
CREATE INDEX IF NOT EXISTS conversations_user ON conversations(user_id);
""")

_cache: "OrderedDict[tuple[int, int], list[dict]]" = OrderedDict()
_sizes: dict[tuple[int, int], int] = {}
_owners: dict[tuple[int, int], int] = {}
_total = 0


def _history_chars(history: list[dict]) -> int:
    total = 0
    for message in history:
        content = message.get("content", "")
        if isinstance(content, str):
            total += len(content)
        else:
            for part in content:
                total += len(part.get("text", ""))
                total += len((part.get("image_url") or {}).get("url", ""))
    return total


def _text_only(message: dict) -> dict:
    content = message.get("content", "")
    if isinstance(content, str):
        return message
    parts = [p.get("text", "") if p.get("type") == "text" else t("observed_image") for p in content]
    return {**message, "content": "\n".join(p for p in parts if p)}


def get(chat_id: int, message_id: int) -> list[dict] | None:
    key = (chat_id, message_id)
    history = _cache.get(key)
    if history is not None:
        return history
    row = db().execute("SELECT messages FROM conversations WHERE chat_id = ? AND msg_id = ?", key).fetchone()
    if row is None:
        return None
    return [prompt.system_message()] + json.loads(row[0])


def _evict(key: tuple[int, int]) -> None:
    global _total
    _cache.pop(key, None)
    _owners.pop(key, None)
    _total -= _sizes.pop(key, 0)


def remember(chat_id: int, message_id: int, history: list[dict], user_id: int | None = None) -> None:
    global _total
    key = (chat_id, message_id)
    _evict(key)
    size = _history_chars(history)
    _cache[key] = history
    _sizes[key] = size
    if user_id is not None:
        _owners[key] = user_id
    _total += size
    while len(_cache) > 1 and (
        len(_cache) > config.CONVERSATION_CACHE_SIZE or _total > config.CONVERSATION_CONTENT_BUDGET
    ):
        _evict(next(iter(_cache)))
    now = time.time()
    stored = [_text_only(m) for m in history if m.get("role") != "system"]
    conn = db()
    conn.execute("INSERT OR REPLACE INTO conversations(chat_id, msg_id, user_id, ts, messages) VALUES (?, ?, ?, ?, ?)",
                 (chat_id, message_id, user_id, now, json.dumps(stored, ensure_ascii=False)))
    conn.execute("DELETE FROM conversations WHERE ts < ?", (now - config.CONVERSATION_KEEP_DAYS * 86400,))


def forget_chat(chat_id: int) -> int:
    for key in [k for k in _cache if k[0] == chat_id]:
        _evict(key)
    return db().execute("DELETE FROM conversations WHERE chat_id = ?", (chat_id,)).rowcount


def forget_user(user_id: int) -> int:
    for key in [k for k, owner in _owners.items() if owner == user_id]:
        _evict(key)
    return db().execute("DELETE FROM conversations WHERE user_id = ?", (user_id,)).rowcount


def clear() -> None:
    """清空两层存储（测试用）。"""
    global _total
    _cache.clear()
    _sizes.clear()
    _owners.clear()
    _total = 0
    db().execute("DELETE FROM conversations")


def trim(history: list[dict]) -> list[dict]:
    """保留 system 消息 + 最近 config.MAX_HISTORY 条对话，且截断后第一条对话必须是 user。

    以 assistant 开头的历史会被 Claude 原生接口和多数本地模型的对话模板
    （要求 user/assistant 严格交替）拒绝，也会丢掉那条回答对应的问题。
    """
    if len(history) <= config.MAX_HISTORY + 1:
        return history
    tail = history[-config.MAX_HISTORY:]
    start = next((i for i, m in enumerate(tail) if m["role"] == "user"), len(tail))
    return [history[0]] + tail[start:]
