"""长期记忆（按聊天分开），参照 Hermes Agent 和 OpenClaw 的做法分两层：

- 原始记录（store.messages）：bot 见到的问答和旁听群的消息，原样存进 SQLite 全文索引，
  模型用 recall 工具查原话；保留 MEMORY_ARCHIVE_DAYS 天。
- 精选条目（store.notes）：群笔记和每个人的档案，一条一条存、各有字数上限。模型在聊天中用
  memory 工具增改删；后台（consolidate）定期读新记录，提出增改删逐条执行，不整段重写。

新对话开头把群笔记、提问者和被引用者的档案、最近几条提问作为附注注入首条 user 消息，
之后的追问沿用同一份历史，前缀不变，上游 prompt 缓存仍能命中。
"""

import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta

from .. import config, jsonstore
from ..i18n import t
from . import consolidate, notes, store

logger = logging.getLogger(__name__)

ASK_CHARS = 1000  # 存进原始记录的单条提问/群消息上限
REPLY_CHARS = 1500  # 存进原始记录的 bot 回答上限
RECENT_SECONDS = 3 * 86400  # 注入「最近的提问」只看这么久以内的
RECENT_COUNT = 5
TICK = 60  # 后台维护间隔（秒）

_maintenance: asyncio.Task | None = None


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def is_observed(chat_id: int) -> bool:
    return config.MEMORY_ENABLED and chat_id in config.MEMORY_OBSERVE_CHATS


def record_exchange(chat_id: int, user_id: int, who: str, question: str, answer: str,
                    quoted: dict | None = None, msg_id: int | None = None) -> None:
    """记下一轮问答。bot 的回答记在提问者名下：/forgetme 时一起删。
    quoted 是被引用的那条消息（user_id/who/text/msg_id/ts）：按它真正的作者单独记一条，
    提问里只写「回复了谁」，不把别人的话混进提问者的提问里。"""
    if not config.MEMORY_ENABLED or not answer.strip():
        return
    now = time.time()
    if quoted and quoted.get("text"):
        store.add_message(chat_id, quoted.get("user_id"), quoted["who"], "chat", _clip(quoted["text"], ASK_CHARS),
                          quoted.get("ts") or now, quoted.get("msg_id"))
    store.add_message(chat_id, user_id, who, "ask", _clip(question, ASK_CHARS), now, msg_id)
    store.add_message(chat_id, user_id, "", "reply", _clip(answer, REPLY_CHARS), now)


def observe(chat_id: int, user_id: int | None, who: str, text: str, msg_id: int | None = None,
            ts: float | None = None) -> None:
    """旁听群里一条没叫到 bot 的消息：只存进原始记录，不请求模型。"""
    text = _clip(text, ASK_CHARS)
    if text and is_observed(chat_id):
        store.add_message(chat_id, user_id, who, "chat", text, ts, msg_id)


def memory_block(chat_id: int, user_ids: list[int]) -> str:
    """注入新对话的附注：群笔记、指定几个人（提问者、被引用者）的档案、最近几条提问；都没有时返回空串。"""
    if not config.MEMORY_ENABLED:
        return ""
    now = time.time()
    parts = []
    curated = notes.render(chat_id, user_ids, now)
    if curated:
        parts.append(curated)
    recent = store.search_messages(chat_id, since=now - RECENT_SECONDS, kinds=("ask",), limit=RECENT_COUNT)
    if recent:
        parts.append(t("memory_recent") + "\n" + "\n".join(
            t("memory_brief_line", who=m.who, q=_clip(m.text, 120), age=notes.age(now - m.ts)) for m in recent))
    if not parts:
        return ""
    return "[" + t("memory_header") + "\n" + "\n".join(parts) + "]"


def clear(chat_id: int) -> bool:
    return store.forget_chat(chat_id)


def forget_user(user_id: int) -> int:
    return store.forget_user(user_id)


# ---- 后台维护 ----

async def _loop() -> None:
    while True:
        await asyncio.sleep(TICK)
        try:
            await consolidate.tick()
        except Exception:
            logger.exception("记忆整理出错")


def start_maintenance() -> None:
    """迁移旧版记忆，并在事件循环里启动定期整理。"""
    global _maintenance
    if not config.MEMORY_ENABLED or _maintenance is not None:
        return
    migrate_legacy()
    _maintenance = asyncio.create_task(_loop())
    logger.info("长期记忆已开启：%s（每个聊天最多每 %d 分钟整理一次，原始记录保留 %d 天）",
                config.MEMORY_DB, config.MEMORY_CONSOLIDATE_INTERVAL, config.MEMORY_ARCHIVE_DAYS)
    if config.MEMORY_OBSERVE_CHATS:
        logger.info("静默旁听：群 %s", ",".join(map(str, sorted(config.MEMORY_OBSERVE_CHATS))))


async def stop_maintenance() -> None:
    """应用退出前停掉定期整理：不停的话事件循环关闭时会报「Task was destroyed but it is pending」。"""
    global _maintenance
    if _maintenance is None:
        return
    _maintenance.cancel()
    await asyncio.wait([_maintenance])
    _maintenance = None


# ---- 旧版 memory.json 迁移 ----

_LEGACY_DATE_RE = re.compile(r"\s*[（(]\s*(?:(\d{4})-)?(\d{1,2})-(\d{1,2})\s*[)）]\s*$")
_LEGACY_LASTING = ("[长期]", "[lasting]")
_LEGACY_ID_RE = re.compile(r"id (\d+)")
LEGACY_FRESH_DAYS = 14  # 旧摘要里一时的话题：从最后一次提到起再保留这么多天


def _legacy_note(line: str, today: date) -> tuple[str, float | None] | None:
    """旧摘要的一行 → (正文, 到期时间)；已经过期的一时话题返回 None。"""
    lasting = any(tag in line for tag in _LEGACY_LASTING)
    for tag in _LEGACY_LASTING:
        line = line.replace(tag, "")
    m = _LEGACY_DATE_RE.search(line)
    when = today
    if m:
        line = line[: m.start()]
        try:
            when = date(int(m.group(1)) if m.group(1) else today.year, int(m.group(2)), int(m.group(3)))
            if not m.group(1) and when > today:
                when = when.replace(year=when.year - 1)
        except ValueError:
            when = today
    text = " ".join(line.strip(" -•").split())[: notes.NOTE_CHARS]
    if not text:
        return None
    if lasting:
        return text, None
    expires = datetime.combine(when + timedelta(days=LEGACY_FRESH_DAYS), datetime.min.time(), config.BOT_TZ)
    return (text, expires.timestamp()) if expires.timestamp() > time.time() else None


def migrate_legacy() -> None:
    """把旧版 memory.json（每个聊天一段摘要 + 最近问答 + 旁听缓冲）搬进新存储，然后改名留底。"""
    path = config.MEMORY_FILE
    if not path.exists():
        return
    legacy = jsonstore.load(path, {}, dict)
    now = time.time()
    today = datetime.fromtimestamp(now, config.BOT_TZ).date()
    moved = 0
    for key, entry in legacy.items():
        try:
            chat_id = int(key)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        for line in str(entry.get("digest", "")).splitlines():
            note = _legacy_note(line, today)
            if note:
                store.insert_note(chat_id, None, "", note[0], note[1], "migrated", now)
                moved += 1
        for r in entry.get("recent") or []:
            uid = _LEGACY_ID_RE.search(r.get("who", ""))
            uid = int(uid.group(1)) if uid else None
            store.add_message(chat_id, uid, r.get("who", ""), "ask", r.get("q", ""), r.get("t", now))
            store.add_message(chat_id, uid, "", "reply", r.get("a", ""), r.get("t", now))
            moved += 1
        for r in entry.get("observed") or []:
            uid = _LEGACY_ID_RE.search(r.get("who", ""))
            store.add_message(chat_id, int(uid.group(1)) if uid else None, r.get("who", ""), "chat",
                              r.get("text", ""), r.get("t", now))
            moved += 1
    path.replace(path.with_name(path.name + ".migrated"))
    logger.info("旧版记忆已迁移：%d 条，原文件改名为 %s.migrated", moved, path.name)
