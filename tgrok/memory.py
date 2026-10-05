"""长期记忆：每个群/私聊一份「记忆摘要 + 最近几轮问答」。

新对话开头把它作为系统附注注入，模型就知道以前聊过什么；注入内容总字数受
MEMORY_MAX_CHARS 限制（摘要最多占一半），控制每次请求多花的 token。
最近问答攒到 RECENT_TRIGGER 条时，在后台让模型把较早的几条并进摘要，不阻塞回复。
记忆写在 MEMORY_FILE（JSON），重启不丢。
"""

import asyncio
import json
import logging
import time

from . import config, llm
from .i18n import t

logger = logging.getLogger(__name__)

RECENT_TRIGGER = 6  # 最近问答达到这么多条就压缩进摘要
RECENT_KEEP = 2  # 压缩后保留原文的最新条数
RECENT_HARD_CAP = 18  # 压缩一直失败时最多保留的原文条数，防止无限增长
Q_CHARS = 150
A_CHARS = 200

_compressing: set[str] = set()
_tasks: set[asyncio.Task] = set()


def _load() -> dict[str, dict]:
    path = config.MEMORY_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        logger.warning("记忆文件 %s 读取失败，从空记忆开始", path)
        return {}


_store: dict[str, dict] = _load() if config.MEMORY_ENABLED else {}


def _save() -> None:
    path = config.MEMORY_FILE
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(_store, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # 原子替换：写到一半崩溃也不会留下半截文件
    except OSError as e:
        logger.warning("记忆文件 %s 写入失败：%s", path, e)


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _line(r: dict) -> str:
    return t("memory_line", who=r["who"], q=r["q"], a=r["a"])


def memory_block(chat_id: int) -> str:
    """注入给模型的记忆附注；没有记忆或未开启时返回空串。
    摘要最多占 MEMORY_MAX_CHARS 的一半，剩余额度从最新往旧填最近问答。"""
    if not config.MEMORY_ENABLED:
        return ""
    entry = _store.get(str(chat_id))
    if not entry or not (entry.get("digest") or entry.get("recent")):
        return ""
    budget = config.MEMORY_MAX_CHARS
    digest = entry.get("digest", "")[: budget // 2]
    used, lines = len(digest), []
    for r in reversed(entry.get("recent", [])):
        line = _line(r)
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    parts = [t("memory_header")]
    if digest:
        parts.append(digest)
    if lines:
        parts.append(t("memory_recent"))
        parts.extend(reversed(lines))
    return "[" + "\n".join(parts) + "]"


def record(chat_id: int, who: str, question: str, answer: str) -> None:
    """记下一轮问答；攒够条数时在后台压缩进摘要。"""
    if not config.MEMORY_ENABLED or not answer.strip():
        return
    key = str(chat_id)
    entry = _store.setdefault(key, {"digest": "", "recent": []})
    entry["recent"].append({"who": _clip(who, 32), "q": _clip(question, Q_CHARS),
                            "a": _clip(answer, A_CHARS), "t": int(time.time())})
    del entry["recent"][:-RECENT_HARD_CAP]
    _save()
    if len(entry["recent"]) >= RECENT_TRIGGER and key not in _compressing:
        _compressing.add(key)
        task = asyncio.create_task(_compress(key, entry))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def _compress(key: str, entry: dict) -> None:
    try:
        batch = entry["recent"][:-RECENT_KEEP]
        if not batch:
            return
        digest = await _summarize(entry.get("digest", ""), batch)
        if digest is None or _store.get(key) is not entry:
            return  # 压缩失败，或期间记忆被 /forget 清掉：都不写回
        # 压缩期间可能又追加了新问答（只会追加在末尾），按条数去掉已并入摘要的那批
        entry["recent"] = entry["recent"][len(batch):]
        entry["digest"] = digest[: config.MEMORY_MAX_CHARS // 2]
        _save()
        logger.info("记忆已压缩 chat=%s：摘要 %d 字，保留最近 %d 条", key, len(entry["digest"]), len(entry["recent"]))
    finally:
        _compressing.discard(key)


async def _noop(_text: str) -> None:
    pass


async def _summarize(old_digest: str, batch: list[dict]) -> str | None:
    """让模型把旧摘要和一批问答合成新摘要；主模型失败时用备用模型，都失败返回 None。"""
    history = [
        {"role": "system", "content": t("memory_summarizer", limit=config.MEMORY_MAX_CHARS // 2)},
        {"role": "user", "content": t("memory_summarize_input", digest=old_digest or t("memory_none"),
                                      lines="\n".join(_line(r) for r in batch))},
    ]
    for adapter in (llm.adapter, llm.fallback_adapter):
        if adapter is None:
            continue
        try:
            result = await adapter.run_round(history, False, _noop)
        except Exception as e:
            logger.warning("记忆压缩调用 %s 失败：%s: %.200s", getattr(adapter, "model", ""), type(e).__name__, e)
            continue
        text = result.content.strip()
        if text:
            return text
    return None


def clear(chat_id: int) -> bool:
    removed = _store.pop(str(chat_id), None) is not None
    if removed:
        _save()
    return removed
