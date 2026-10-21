"""斜杠命令。COMMANDS 是唯一的命令表：注册处理函数和 Telegram 命令菜单都从这里生成。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telegram import Update
from telegram.ext import ContextTypes

from . import autoreply, config, conversations, memory, recent, render, rich, slang, usage
from .memory import notes, store
from .i18n import t
from .prompt import person_label
from .tg_auth import allowed_users, is_admin, is_authorized, save_allowed_users

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def _authorized(update: Update) -> bool:
    user = update.effective_user
    return user is not None and is_authorized(user.id)


def _admin(update: Update) -> bool:
    user = update.effective_user
    return user is not None and is_admin(user.id)


def _manages_memory(update: Update) -> bool:
    """管理员在任何聊天都能看/清记忆和梗词典；私聊里白名单用户可以管自己的。"""
    chat = update.effective_chat
    return chat is not None and (_admin(update) or (chat.type == "private" and _authorized(update)))


async def _reply(update: Update, text: str) -> None:
    msg = update.effective_message
    await rich.send_rich(msg, render.plain_to_html(text), text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, t("start", username=context.bot.username, user_id=update.effective_user.id))


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not config.MEMORY_ENABLED:
        await _reply(update, t("memory_disabled"))
        return
    chat_id = update.effective_chat.id
    block = notes.render_all(chat_id)
    lines = [t("memory_show", block=block) if block else t("memory_empty"),
             t("memory_archive", n=store.count_messages(chat_id), days=config.MEMORY_ARCHIVE_DAYS),
             t("memory_chat_id", chat_id=chat_id)]
    await _reply(update, "\n\n".join(lines)[:4000])


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/forget 清空本聊天的记忆和聊天记录；/forget 12 15 只删这几条（编号见 /memory）。"""
    chat_id = update.effective_chat.id
    ids = [a.lstrip("#") for a in context.args or []]
    if not ids:
        removed = [memory.clear(chat_id), conversations.forget_chat(chat_id), recent.forget_chat(chat_id)]
        await _reply(update, t("memory_cleared") if any(removed) else t("memory_empty"))
        return
    try:
        notes.apply(chat_id, [{"action": "remove", "id": i} for i in ids], "command")
    except notes.NoteError as e:
        await _reply(update, str(e))
        return
    await _reply(update, t("memory_removed", ids=", ".join(f"#{i}" for i in ids)))


async def cmd_forgetme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    removed = memory.forget_user(user_id) + conversations.forget_user(user_id) + recent.forget_user(user_id)
    await _reply(update, t("forgetme_done", n=removed) if removed else t("forgetme_none"))


async def cmd_slang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not config.SLANG_ENABLED:
        await _reply(update, t("slang_disabled"))
        return
    items = slang.entries_for(update.effective_chat.id)
    if not items:
        await _reply(update, t("slang_none"))
        return
    lines = "\n".join(slang.describe(v) for v in items)
    await _reply(update, t("slang_list", n=len(items), lines=lines)[:4000])


_GLOBAL_WORDS = ("通用", "global")


async def cmd_learn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """白名单用户都能教：/learn 词=解释|语境（本群），/learn 通用 词=解释（所有群）。"""
    parts = (update.effective_message.text or "").split(maxsplit=1)
    raw = parts[1] if len(parts) > 1 else ""
    scope = "chat"
    head, _, rest = raw.partition(" ")
    if head.lower() in _GLOBAL_WORDS:
        scope, raw = slang.GLOBAL, rest
    term, sep, rest = raw.replace("＝", "=").partition("=")
    meaning, _, usage = rest.replace("｜", "|").partition("|")
    saved = slang.learn(term, meaning, update.effective_chat.id, scope=scope, context=usage,
                        by=person_label(update.effective_user)) if sep else None
    await _reply(update, t("slang_learned", term=saved) if saved else t("slang_usage"))


async def cmd_unlearn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    term = " ".join(context.args or [])
    if not term:
        await _reply(update, t("slang_usage"))
        return
    removed = slang.forget(term, update.effective_chat.id)
    await _reply(update, t("slang_forgotten" if removed else "slang_not_found", term=term))


def _target_user_ids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[set[int], str | None]:
    """管理命令的目标用户：优先取命令参数里的 ID，否则取被回复消息的发送者。"""
    ids = set()
    for arg in context.args or []:
        try:
            ids.add(int(arg.strip().rstrip(",，")))
        except ValueError:
            return set(), t("invalid_id", arg=arg)
    if not ids:
        replied = update.effective_message.reply_to_message
        if replied and replied.from_user:
            ids.add(replied.from_user.id)
    if not ids:
        return set(), t("admin_usage")
    return ids, None


async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ids, err = _target_user_ids(update, context)
    if err:
        await _reply(update, err)
        return
    allowed_users.update(ids)
    save_allowed_users()
    await _reply(update, t("added", ids=", ".join(map(str, sorted(ids))), n=len(allowed_users)))


async def cmd_deluser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ids, err = _target_user_ids(update, context)
    if err:
        await _reply(update, err)
        return
    removed = ids & allowed_users
    allowed_users.difference_update(ids)
    save_allowed_users()
    await _reply(update, t("removed", ids=", ".join(map(str, sorted(removed))) if removed else t("no_match"),
                           n=len(allowed_users)))


async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [t("admins", ids=", ".join(map(str, sorted(config.ADMIN_USER_IDS))) or t("not_configured"))]
    if allowed_users:
        lines.append(t("whitelist", n=len(allowed_users), ids="\n".join(map(str, sorted(allowed_users)))))
    else:
        lines.append(t("whitelist_empty_controlled") if config.ADMIN_USER_IDS else t("whitelist_empty_open"))
    await _reply(update, "\n".join(lines))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, usage.report()[:4000])


_DELETE_WORDS = ("del", "rm", "删", "删除")


async def cmd_kv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """关键词直接回复（管理员）：/kv 列出；/kv 词=回复 新增或覆盖（回复可以多行）；
    回复某条消息发 /kv 词，用那条消息的文字当回复；/kv del 词 删除。"""
    msg = update.effective_message
    parts = (msg.text or "").split(maxsplit=1)
    raw = parts[1].strip() if len(parts) > 1 else ""
    if not raw:
        items = autoreply.entries()
        lines = "\n".join(t("kv_line", trigger=k, reply=" ".join(v.split())[:60]) for k, v in items)
        await _reply(update, t("kv_list", n=len(items), lines=lines)[:4000] if items else t("kv_empty"))
        return
    head, _, rest = raw.partition(" ")
    if head.lower() in _DELETE_WORDS and rest.strip():
        removed = autoreply.remove(rest)
        await _reply(update, t("kv_removed" if removed else "kv_not_found", trigger=rest.strip()))
        return
    cut = min((i for i in (raw.find("="), raw.find("＝")) if i >= 0), default=-1)
    trigger, sep, reply = (raw[:cut], True, raw[cut + 1:]) if cut >= 0 else (raw, False, "")
    if not sep:
        replied = msg.reply_to_message
        reply = (replied.text or replied.caption or "") if replied else ""
    saved = autoreply.put(trigger, reply)
    await _reply(update, t("kv_saved", trigger=saved) if saved else t("kv_usage"))


@dataclass(frozen=True)
class Command:
    names: tuple[str, ...]
    handler: Handler
    allowed: Callable[[Update], bool]  # 不满足时静默忽略
    menu: str  # 菜单里的说明（i18n key）
    admin_menu_only: bool  # True：只出现在管理员私聊的命令菜单里

    async def __call__(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.allowed(update):
            await self.handler(update, context)


COMMANDS = [
    Command(("help", "start"), cmd_start, _authorized, "cmd_help", False),
    Command(("learn",), cmd_learn, _authorized, "cmd_learn", False),
    Command(("forgetme",), cmd_forgetme, _authorized, "cmd_forgetme", False),
    Command(("adduser",), cmd_adduser, _admin, "cmd_adduser", True),
    Command(("deluser",), cmd_deluser, _admin, "cmd_deluser", True),
    Command(("listusers",), cmd_listusers, _admin, "cmd_listusers", True),
    Command(("stats",), cmd_stats, _admin, "cmd_stats", True),
    Command(("kv",), cmd_kv, _admin, "cmd_kv", True),
    Command(("memory",), cmd_memory, _manages_memory, "cmd_memory", True),
    Command(("forget",), cmd_forget, _manages_memory, "cmd_forget", True),
    Command(("slang",), cmd_slang, _manages_memory, "cmd_slang", True),
    Command(("unlearn",), cmd_unlearn, _manages_memory, "cmd_unlearn", True),
]
