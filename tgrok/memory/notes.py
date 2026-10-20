"""精选条目：增改删的校验和容量限制，以及给模型看的渲染。

条目分两种：群笔记（user_id 为空）和某人在这个聊天的档案。每种按聊天（档案再按人）有总字数上限，
一批操作按最终结果检查容量：超了就整批不执行，返回现有条目，让模型先合并或删掉旧的（同 Hermes）。
一时的事写入时带天数，到期后不再注入，由后台清掉。
"""

import re
import time
from dataclasses import dataclass, field

from .. import config
from ..i18n import t
from . import store

NOTE_CHARS = 200  # 单条上限
_INVISIBLE_RE = re.compile("[\u0000-\u0008\u000b-\u001f\u007f\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


class NoteError(ValueError):
    """一批操作不能执行；str(e) 是给模型看的说明。"""


@dataclass
class Result:
    added: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # 重复等无需执行的原因


def _clean(text) -> str:
    return " ".join(_INVISIBLE_RE.sub("", str(text or "")).split())


def _expires(days, now: float) -> float | None:
    if days in (None, "", 0):
        return None
    try:
        days = float(days)
    except (TypeError, ValueError):
        raise NoteError(t("note_bad_days", days=days)) from None
    if days <= 0 or days > 3650:
        raise NoteError(t("note_bad_days", days=days))
    return now + days * 86400


def _limit(user_id: int | None) -> int:
    return config.MEMORY_GROUP_CHARS if user_id is None else config.MEMORY_PROFILE_CHARS


def apply(chat_id: int, operations: list, source: str, who_of: dict[int, str] | None = None,
          now: float | None = None) -> Result:
    """执行一批操作（全部成功或全部不执行）。

    operations 里每项：{"action": "add", "target": "group"|"user", "user_id": 数字（档案用）, "text": ..., "days": 可选}、
    {"action": "update", "id": 条目号, "text": 可选, "days": 可选（0 = 改成长期）}、{"action": "remove", "id": 条目号}。
    who_of 提供档案主人的说话人标识（显示用）。
    """
    now = time.time() if now is None else now
    if not isinstance(operations, list) or not operations:
        raise NoteError(t("note_no_ops"))
    current = {n.id: n for n in store.notes(chat_id, now)}
    # 先在内存里模拟一遍：校验、查重、算容量
    sim = {nid: (n.user_id, n.text) for nid, n in current.items()}
    plan = []
    result = Result()
    for raw in operations:
        op = raw if isinstance(raw, dict) else {}
        action = str(op.get("action", "")).strip().lower()
        if action == "add":
            text = _clean(op.get("text"))
            if not text:
                raise NoteError(t("note_empty"))
            if len(text) > NOTE_CHARS:
                raise NoteError(t("note_too_long", n=len(text), limit=NOTE_CHARS))
            target = str(op.get("target", "")).strip().lower()
            if target == "group":
                user_id = None
            elif target == "user":
                try:
                    user_id = int(op.get("user_id"))
                except (TypeError, ValueError):
                    raise NoteError(t("note_need_user")) from None
            else:
                raise NoteError(t("note_bad_target", target=target))
            if any(uid == user_id and existing == text for uid, existing in sim.values()):
                result.skipped.append(text)
                continue
            key = f"new{len(plan)}"
            sim[key] = (user_id, text)
            plan.append(("add", user_id, text, _expires(op.get("days"), now)))
        elif action in ("update", "remove"):
            try:
                note_id = int(op.get("id"))
            except (TypeError, ValueError):
                raise NoteError(t("note_need_id")) from None
            if note_id not in sim:
                raise NoteError(t("note_unknown_id", id=note_id))
            if action == "remove":
                del sim[note_id]
                plan.append(("remove", note_id))
                continue
            user_id, old_text = sim[note_id]
            text = _clean(op.get("text")) or old_text
            if len(text) > NOTE_CHARS:
                raise NoteError(t("note_too_long", n=len(text), limit=NOTE_CHARS))
            expires = current[note_id].expires if "days" not in op else _expires(op.get("days"), now)
            sim[note_id] = (user_id, text)
            plan.append(("update", note_id, text, expires))
        else:
            raise NoteError(t("note_bad_action", action=action))
    for owner in {uid for uid, _ in sim.values()}:
        used = sum(len(text) for uid, text in sim.values() if uid == owner)
        if used > _limit(owner):
            entries = [n for n in current.values() if n.user_id == owner]
            raise NoteError(t("note_full", store=_store_name(owner, who_of, entries), used=used,
                              limit=_limit(owner), over=used - _limit(owner))
                            + "\n" + "\n".join(render_line(n, now) for n in entries))
    for step in plan:
        if step[0] == "add":
            _, user_id, text, expires = step
            who = (who_of or {}).get(user_id, "") if user_id is not None else ""
            if user_id is not None and not who:
                who = next((n.who for n in current.values() if n.user_id == user_id and n.who), "") \
                    or store.latest_who(chat_id, user_id)
            result.added.append(store.insert_note(chat_id, user_id, who, text, expires, source, now))
        elif step[0] == "update":
            _, note_id, text, expires = step
            store.update_note(note_id, text, expires, now)
            result.updated.append(note_id)
        else:
            store.delete_note(step[1])
            result.removed.append(step[1])
    return result


def _store_name(user_id: int | None, who_of: dict[int, str] | None, entries: list) -> str:
    if user_id is None:
        return t("notes_group")
    who = (who_of or {}).get(user_id) or next((n.who for n in entries if n.who), "") or f"id {user_id}"
    return t("notes_profile", who=who)


def age(seconds: float) -> str:
    """距今多久，给模型看的说法：刚才、3 小时前、昨天、5 天前、约 2 个月前。"""
    if seconds < 3600:
        return t("age_just_now")
    if seconds < 86400:
        return t("age_hours", n=int(seconds // 3600))
    days = int(seconds // 86400)
    if days == 1:
        return t("age_yesterday")
    if days < 60:
        return t("age_days", n=days)
    if days < 365:
        return t("age_months", n=days // 30)
    return t("age_years", n=days // 365)


def render_line(note: store.Note, now: float) -> str:
    """一条条目：#编号 正文（多久前记的；一时的事标还剩几天）。"""
    meta = age(now - note.updated)
    if note.expires is not None:
        meta += t("note_expires_in", days=max(1, round((note.expires - now) / 86400)))
    return f"#{note.id} {note.text}（{meta}）"


def render(chat_id: int, user_ids: list[int], now: float | None = None) -> str:
    """群笔记 + 指定几个人的档案，带容量；都没有时返回空串。"""
    now = time.time() if now is None else now
    all_notes = store.notes(chat_id, now)
    sections = []
    group = [n for n in all_notes if n.user_id is None]
    if group:
        used = sum(len(n.text) for n in group)
        sections.append(t("notes_group_header", used=used, limit=config.MEMORY_GROUP_CHARS)
                        + "\n" + "\n".join(render_line(n, now) for n in group))
    for uid in dict.fromkeys(user_ids):
        mine = [n for n in all_notes if n.user_id == uid]
        if mine:
            used = sum(len(n.text) for n in mine)
            # 名字取聊天记录里最近的说话人标识（改了名也跟着变）；还没说过话就用条目上记的
            who = (store.latest_who(chat_id, uid) or next((n.who for n in reversed(mine) if n.who), "")
                   or f"id {uid}")
            sections.append(t("notes_profile_header", who=who, used=used, limit=config.MEMORY_PROFILE_CHARS)
                            + "\n" + "\n".join(render_line(n, now) for n in mine))
    return "\n".join(sections)


def render_all(chat_id: int, now: float | None = None) -> str:
    """一个聊天里的全部条目（/memory 用）。"""
    now = time.time() if now is None else now
    owners = [n.user_id for n in store.notes(chat_id, now) if n.user_id is not None]
    return render(chat_id, owners, now)
