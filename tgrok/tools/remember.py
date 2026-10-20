"""memory：模型在聊天中增改删这个聊天的精选记忆（群笔记、每个人的档案）。"""

from .. import config
from ..i18n import t
from ..memory import notes
from . import Outcome, Tool, failed

_OPERATION = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["add", "update", "remove"]},
        "target": {"type": "string", "enum": ["group", "user"],
                   "description": "add only: group = group notes, user = that person's profile."},
        "user_id": {"type": "integer", "description": "add to a profile: the person's numeric id from the speaker label."},
        "id": {"type": "integer", "description": "update/remove: the entry number shown as #number."},
        "text": {"type": "string", "description": "add/update: the entry, one fact or a few related facts, short."},
        "days": {"type": "integer", "description": "Optional: a passing matter expires after this many days; "
                                                   "omit for lasting facts (on update, 0 makes it lasting)."},
    },
    "required": ["action"],
}


class RememberTool(Tool):
    name = "memory"
    description = (
        "Add, update, or remove entries in this chat's long-term memory: group notes and each person's profile. "
        "Entries are shown as #number at the start of new conversations. All operations in one call succeed or "
        "fail together; if a store would go over its character limit, nothing is applied and the current entries "
        "are returned so you can merge or remove old ones in the same batch."
    )
    parameters = {
        "type": "object",
        "properties": {"operations": {"type": "array", "items": _OPERATION}},
        "required": ["operations"],
    }

    def enabled(self) -> bool:
        return config.MEMORY_ENABLED

    def prompt_note(self) -> str:
        return t("memory_tool_prompt")

    def label(self, args: dict) -> str:
        ops = args.get("operations") if isinstance(args.get("operations"), list) else []
        counts = {}
        for op in ops:
            action = str(op.get("action", "")) if isinstance(op, dict) else ""
            if action in ("add", "update", "remove"):
                counts[action] = counts.get(action, 0) + 1
        what = "、".join(t(f"tool_memory_{a}", n=n) for a, n in counts.items()) or "?"
        return t("tool_memory", what=what)

    async def run(self, args: dict, chat_id: int | None) -> Outcome:
        if chat_id is None:
            return failed(t("note_no_ops"))
        ops = args.get("operations")
        try:
            result = notes.apply(chat_id, ops, "tool")
        except notes.NoteError as e:
            return failed(str(e))
        owners = [op.get("user_id") for op in ops if isinstance(op, dict) and isinstance(op.get("user_id"), int)]
        skipped = t("memory_tool_skipped", n=len(result.skipped)) if result.skipped else ""
        content = t("memory_tool_done", added=len(result.added), updated=len(result.updated),
                    removed=len(result.removed), skipped=skipped,
                    notes=notes.render(chat_id, owners) or t("notes_none"))
        return Outcome(content, True, t("res_memory"))
