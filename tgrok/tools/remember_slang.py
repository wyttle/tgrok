"""remember_slang：模型弄明白一个梗或黑话的用法后记进词典。"""

from .. import config, slang
from ..i18n import t
from . import Outcome, Tool, failed


class RememberSlangTool(Tool):
    name = "remember_slang"
    description = (
        "Save one usage of a meme, slang term, abbreviation, metaphor, or in-joke, so next time it comes up "
        "you have a reference. Call it right after you've worked out what an expression you didn't know or had "
        "wrong really means in context: from a page that explains it, or because someone corrected you or "
        "explained it. Chinese expressions shift with context, so record the meaning as used in that situation "
        "(including the ironic or hidden layer), when it carries that meaning, and the original message. A term "
        "can hold several usages; saving a new one keeps the others. Don't save ordinary words or things you "
        "were already sure about."
    )
    parameters = {
        "type": "object",
        "properties": {
            "term": {"type": "string", "description": "The expression exactly as people write it, 1-32 characters."},
            "meaning": {
                "type": "string",
                "description": "What it means in this usage, in one or two short sentences "
                               "(origin in a few words if it helps), in the chat's language.",
            },
            "context": {
                "type": "string",
                "description": "When it carries this meaning and how to tell it apart from the literal or other "
                               "readings: who says it, about what, in reply to what, with what tone.",
            },
            "example": {"type": "string", "description": "The message where it was used this way, quoted briefly."},
            "scope": {
                "type": "string",
                "enum": ["global", "chat"],
                "description": "global = a general internet meme or saying; chat = an in-joke or usage specific to this group.",
            },
        },
        "required": ["term", "meaning", "context", "scope"],
    }

    def enabled(self) -> bool:
        return config.SLANG_ENABLED

    def prompt_note(self) -> str:
        return t("slang_system_prompt")

    def label(self, args: dict) -> str:
        return t("tool_slang", term=(str(args.get("term", "")).strip() or "?")[:32])

    async def run(self, args: dict, chat_id: int | None) -> Outcome:
        scope = str(args.get("scope", "chat")).strip().lower()
        term = slang.learn(str(args.get("term", "")), str(args.get("meaning", "")), chat_id,
                           scope=slang.GLOBAL if scope == slang.GLOBAL else "chat",
                           context=str(args.get("context", "")), example=str(args.get("example", "")))
        if term is None:
            return failed(t("slang_bad_args", min=slang.MIN_TERM_CHARS, max=slang.MAX_TERM_CHARS))
        return Outcome(t("slang_saved", term=term), True, t("res_saved"))
