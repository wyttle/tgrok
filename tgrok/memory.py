"""长期记忆：每个群/私聊一份「记忆摘要 + 最近几轮问答」。

新对话开头把它作为系统附注注入，模型就知道以前聊过什么；注入内容总字数受
MEMORY_MAX_CHARS 限制（摘要最多占一半），控制每次请求多花的 token。
最近的提问只注入 RECENT_TTL 以内的，旧的一句气话不会被带着到处走。
最近问答攒到 RECENT_TRIGGER 条时，在后台让模型把较早的几条并进摘要，不阻塞回复；
摘要每条带日期，不重要的内容久不提起就被模型删掉。
记忆写在 MEMORY_FILE（JSON），重启不丢。
"""

import asyncio
import json
import logging
import time
from datetime import datetime

from . import config, llm
from .i18n import t

logger = logging.getLogger(__name__)

RECENT_TRIGGER = 6  # 最近问答达到这么多条就压缩进摘要
RECENT_KEEP = 2  # 压缩后保留原文的最新条数
RECENT_HARD_CAP = 18  # 压缩一直失败时最多保留的原文条数，防止无限增长
Q_CHARS = 150
A_CHARS = 200
RECENT_TTL = 3 * 86400  # 最近的提问只注入这么久以内的（秒）

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


def _date(ts: float, fmt: str = "%m-%d") -> str:
    return datetime.fromtimestamp(ts, config.BOT_TZ).strftime(fmt)


def _line(r: dict) -> str:
    """给压缩摘要那次调用看的完整一行：日期、说话人、问题和当时的回答。"""
    return t("memory_line", date=_date(r.get("t", time.time())), who=r["who"], q=r["q"], a=r["a"])


def memory_block(chat_id: int) -> str:
    """注入给模型的记忆附注；没有记忆或未开启时返回空串。
    摘要最多占 MEMORY_MAX_CHARS 的一半，剩余额度从最新往旧填最近的提问。
    只放「谁问了什么」、不放 bot 当时的回答：带着旧回答原文，模型容易顺着把旧话题再讲一遍。"""
    if not config.MEMORY_ENABLED:
        return ""
    entry = _store.get(str(chat_id))
    if not entry or not (entry.get("digest") or entry.get("recent")):
        return ""
    budget = config.MEMORY_MAX_CHARS
    digest = entry.get("digest", "")[: budget // 2]
    used, lines = len(digest), []
    cutoff = time.time() - RECENT_TTL
    for r in reversed(entry.get("recent", [])):
        if r.get("t", 0) < cutoff:
            break  # 按时间追加，再往前只会更旧
        line = t("memory_brief_line", who=r["who"], q=r["q"])
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    if not digest and not lines:
        return ""  # 只剩过期的提问：什么都不注入
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
    entry["recent"].append({"who": _clip(who, 64), "q": _clip(question, Q_CHARS),
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
        digest = await _summarize(_qa_input(entry.get("digest", ""), batch))
        if digest is None or _store.get(key) is not entry:
            return  # 压缩失败，或期间记忆被 /forget 清掉：都不写回
        # 压缩期间可能又追加了新问答（只会追加在末尾），按条数去掉已并入摘要的那批
        entry["recent"] = entry["recent"][len(batch):]
        entry["digest"] = digest[: config.MEMORY_MAX_CHARS // 2]
        _save()
        logger.info("记忆已压缩 chat=%s：摘要 %d 字，保留最近 %d 条", key, len(entry["digest"]), len(entry["recent"]))
    finally:
        _compressing.discard(key)


# ---- 静默旁听 ----
# 指定群里 bot 没被叫到的消息只追加到缓冲区，不调用模型；observe_loop 每分钟检查一次，
# 某个群距上次整理满 MEMORY_OBSERVE_INTERVAL 分钟、且攒够 OBSERVE_MIN_MESSAGES 条时才整理一次。
OBSERVE_MIN_MESSAGES = 5
OBSERVE_LINE_CHARS = 200
OBSERVE_TICK = 60  # 检查间隔（秒），顺带把缓冲区落盘
_dirty = False
_observer: asyncio.Task | None = None


def is_observed(chat_id: int) -> bool:
    return config.MEMORY_ENABLED and chat_id in config.MEMORY_OBSERVE_CHATS


def observe(chat_id: int, who: str, text: str) -> None:
    """旁听一条群消息：只进缓冲区，不请求模型、不立刻写盘。缓冲区超出字数上限时丢最旧的。"""
    global _dirty
    text = _clip(text, OBSERVE_LINE_CHARS)
    if not text or not is_observed(chat_id):
        return
    entry = _store.setdefault(str(chat_id), {"digest": "", "recent": []})
    buf = entry.setdefault("observed", [])
    if not buf:
        # 一个整理周期从攒下第一条开始算：安静了很久的群来几条消息，也要等满一个周期才整理
        entry["observed_at"] = int(time.time())
    buf.append({"who": _clip(who, 64), "text": text, "t": int(time.time())})
    total = sum(len(r["text"]) for r in buf)
    while buf and total > config.MEMORY_OBSERVE_MAX_CHARS:
        total -= len(buf.pop(0)["text"])
    _dirty = True


def pending_observed(chat_id: int) -> int:
    entry = _store.get(str(chat_id))
    return len(entry.get("observed", [])) if entry else 0


async def summarize_observed(key: str) -> bool:
    """把一个群攒下的旁听消息并进摘要；成功返回 True。失败时缓冲区原样保留，下个周期再试。"""
    entry = _store.get(key)
    if not entry or not entry.get("observed") or key in _compressing:
        return False
    _compressing.add(key)
    try:
        batch = list(entry["observed"])
        lines = "\n".join(t("memory_observed_line", date=_date(r["t"], "%m-%d %H:%M"), who=r["who"], text=r["text"])
                          for r in batch)
        digest = await _summarize(t("memory_summarize_observed_input", today=_date(time.time(), "%Y-%m-%d"),
                                    digest=entry.get("digest", "") or t("memory_none"), lines=lines))
        if digest is None or _store.get(key) is not entry:
            return False
        # 整理期间可能又攒了新消息，或缓冲区超限丢掉了最旧的：按对象身份去掉已整理的那批
        done = {id(r) for r in batch}
        entry["observed"] = [r for r in entry["observed"] if id(r) not in done]
        entry["digest"] = digest[: config.MEMORY_MAX_CHARS // 2]
        entry["observed_at"] = int(time.time())
        _save()
        logger.info("旁听消息已整理 chat=%s：%d 条 → 摘要 %d 字", key, len(batch), len(entry["digest"]))
        return True
    finally:
        _compressing.discard(key)


async def observe_tick(now: float | None = None) -> None:
    """一次检查：到期且攒够条数的群逐个整理（串行，不并发打模型），然后把缓冲区落盘。"""
    global _dirty
    now = time.time() if now is None else now
    interval = config.MEMORY_OBSERVE_INTERVAL * 60
    for key, entry in list(_store.items()):
        buf = entry.get("observed")
        if not buf or len(buf) < OBSERVE_MIN_MESSAGES:
            continue
        if now - entry.get("observed_at", now) < interval:
            continue
        if not await summarize_observed(key):
            entry["observed_at"] = int(now)  # 失败也等下个周期，别每分钟重试
    if _dirty:
        _dirty = False
        _save()


async def _observe_loop() -> None:
    while True:
        await asyncio.sleep(OBSERVE_TICK)
        try:
            await observe_tick()
        except Exception:
            logger.exception("旁听整理出错")


def start_observer() -> None:
    """在事件循环里启动定期整理；没配置旁听群时什么都不做。"""
    global _observer
    if config.MEMORY_ENABLED and config.MEMORY_OBSERVE_CHATS and _observer is None:
        _observer = asyncio.create_task(_observe_loop())
        logger.info("静默旁听已开启：群 %s，每 %d 分钟最多整理一次",
                    ",".join(map(str, sorted(config.MEMORY_OBSERVE_CHATS))), config.MEMORY_OBSERVE_INTERVAL)


async def _noop(_text: str) -> None:
    pass


def _qa_input(old_digest: str, batch: list[dict]) -> str:
    return t("memory_summarize_input", today=_date(time.time(), "%Y-%m-%d"),
             digest=old_digest or t("memory_none"), lines="\n".join(_line(r) for r in batch))


async def _summarize(input_text: str) -> str | None:
    """让模型把旧摘要和新内容合成新摘要；主模型失败时用备用模型，都失败返回 None。"""
    history = [
        {"role": "system", "content": t("memory_summarizer", limit=config.MEMORY_MAX_CHARS // 2)},
        {"role": "user", "content": input_text},
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
