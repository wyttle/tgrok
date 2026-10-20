"""后台整理：读一个聊天里还没整理过的原始记录，让模型对照现有条目提出增改删，逐条执行。

和旧版「整段重写摘要」不同，模型只提出操作，没提到的条目原样保留，不会越改越走样。
容量超了先把错误交还模型重提一次；还不行就跳过这批，记录照样标为已整理，免得反复卡在同一批上。
"""

import json
import logging
import time
from datetime import datetime

from .. import config, llm
from ..i18n import t
from . import notes, store

logger = logging.getLogger(__name__)

BATCH_CHARS = 12000  # 一次交给模型的原始记录字数上限
REPLY_CHARS = 300  # bot 回答在整理输入里截到这么长：只用来看懂上下文
STALE_SECONDS = 86400  # 新记录不够 MEMORY_CONSOLIDATE_MIN 条，但最早一条已经放了这么久，也整理

_running: set[int] = set()


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, config.BOT_TZ).strftime("%Y-%m-%d %H:%M")


def _line(m: store.Message) -> str:
    if m.kind == "reply":
        return t("curate_reply_line", when=_when(m.ts), text=m.text[:REPLY_CHARS])
    return t("curate_line", when=_when(m.ts), who=m.who, text=m.text)


async def _noop(_text: str) -> None:
    pass


def _parse(text: str) -> list | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    ops = data.get("operations") if isinstance(data, dict) else None
    return ops if isinstance(ops, list) else None


async def _ask(history: list[dict]) -> tuple[str, list] | None:
    """先用 Gemini 帮手模型（不占主模型的并发和额度），失败再用主模型、备用模型；
    返回（原文, 操作列表），都失败返回 None。"""
    for adapter in (llm.helper_adapter, llm.adapter, llm.fallback_adapter):
        if adapter is None:
            continue
        try:
            result = await adapter.run_round(history, None, _noop)
        except Exception as e:
            logger.warning("记忆整理调用 %s 失败：%s: %.200s", getattr(adapter, "model", ""), type(e).__name__, e)
            continue
        ops = _parse(result.content)
        if ops is not None:
            return result.content, ops
        logger.warning("记忆整理：%s 没有给出可解析的操作", getattr(adapter, "model", ""))
    return None


def _batch(chat_id: int, done_id: int) -> list[store.Message]:
    batch, used = [], 0
    for m in store.messages_after(chat_id, done_id, limit=500):
        line = _line(m)
        if batch and used + len(line) > BATCH_CHARS:
            break
        batch.append(m)
        used += len(line)
    return batch


async def consolidate(chat_id: int, now: float | None = None) -> bool:
    """整理一个聊天的一批新记录；成功（包括模型认为无需改动）返回 True。"""
    now = time.time() if now is None else now
    if chat_id in _running:
        return False
    _running.add(chat_id)
    try:
        done_id, _ = store.progress(chat_id)
        batch = _batch(chat_id, done_id)
        if not batch:
            return True
        people = {m.user_id: m.who for m in batch if m.user_id is not None and m.kind != "reply"}
        current = notes.render(chat_id, list(people), now)
        history = [
            {"role": "system", "content": t("curator_prompt", group_limit=config.MEMORY_GROUP_CHARS,
                                             profile_limit=config.MEMORY_PROFILE_CHARS, note_chars=notes.NOTE_CHARS)},
            {"role": "user", "content": t("curate_input", today=_when(now)[:10], notes=current or t("notes_none"),
                                          people="\n".join(f"- {who}" for who in people.values()) or t("notes_none"),
                                          lines="\n".join(_line(m) for m in batch))},
        ]
        answer = await _ask(history)
        if answer is None:
            store.set_progress(chat_id, tried=now)
            return False
        raw, ops = answer
        applied = None
        if ops:
            try:
                applied = notes.apply(chat_id, ops, "consolidate", who_of=people, now=now)
            except notes.NoteError as e:
                history += [{"role": "assistant", "content": raw},
                            {"role": "user", "content": t("curate_retry", error=str(e))}]
                retry = await _ask(history)
                try:
                    applied = notes.apply(chat_id, retry[1], "consolidate", who_of=people, now=now) \
                        if retry and retry[1] else None
                except notes.NoteError as e2:
                    logger.warning("记忆整理 chat=%s 两次都超出容量，跳过这批：%s", chat_id, str(e2)[:200])
        store.set_progress(chat_id, done_id=batch[-1].id, tried=now)
        logger.info("记忆整理 chat=%s：%d 条记录 → 新增 %d、修改 %d、删除 %d", chat_id, len(batch),
                    len(applied.added) if applied else 0, len(applied.updated) if applied else 0,
                    len(applied.removed) if applied else 0)
        return True
    finally:
        _running.discard(chat_id)


async def tick(now: float | None = None) -> None:
    """一次维护：清掉到期条目和保留期外的记录；到期且攒够新记录的聊天逐个整理（串行，不并发打模型）。"""
    now = time.time() if now is None else now
    store.purge_expired_notes(now)
    store.purge_messages(now - config.MEMORY_ARCHIVE_DAYS * 86400)
    for chat_id in store.chats_with_messages():
        done_id, tried = store.progress(chat_id)
        if now - tried < config.MEMORY_CONSOLIDATE_INTERVAL * 60:
            continue
        count, oldest = store.count_after(chat_id, done_id)
        if count >= config.MEMORY_CONSOLIDATE_MIN or (count and now - oldest >= STALE_SECONDS):
            await consolidate(chat_id, now)
