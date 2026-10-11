"""梗和黑话词典：弄明白过一次的说法记下来，下次出现时作为参考附上。

中文的梗、隐喻和黑话跟语境绑得很紧：同一个词换个句子可能是字面意思，也可能是另一层意思。
所以词典不存「词 → 唯一释义」，而是每个词存几种用法（义项），每种都带上「什么语境下是这个意思」
和学到它时的原句。注入时明确告诉模型这只是以前见过的用法，要先看这句话的语境再判断，
也可能就是字面意思。

模型查清一个说法后（或者被群友纠正、解释后）调用 remember_slang 写入；群友也可以用 /learn 手动教。
每条消息只附上里面真正出现了的词，最多 INJECT_LIMIT 个词、每个词最多 MAX_SENSES 种用法。
通用网络梗（scope=global）所有聊天共享，本群专属的说法只在本群用。
词典写在 SLANG_FILE（JSON），词条数超过 SLANG_MAX_ENTRIES 时淘汰最久没用到的。
"""

import json
import logging
import time

from . import config
from .i18n import t

logger = logging.getLogger(__name__)

MIN_TERM_CHARS = 1  # 「典」「急」「润」这类单字梗很常见
SHORT_LINE_CHARS = 12  # 单字词只在这么短的一句话里才算命中，长句里的同一个字多半是普通用法
MAX_TERM_CHARS = 32
MEANING_CHARS = 160
CONTEXT_CHARS = 100
EXAMPLE_CHARS = 80
MAX_SENSES = 3  # 每个词留几种用法：新的在前，被纠正后的说法排在旧理解前面
INJECT_LIMIT = 5
GLOBAL = "global"


def _load() -> dict[str, dict]:
    path = config.SLANG_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        logger.warning("梗词典 %s 读取失败，从空词典开始", path)
        return {}


# key = f"{scope}|{词的小写}"，value = {term, scope, used, senses: [{meaning, context, example, by, t}]}
_entries: dict[str, dict] = _load() if config.SLANG_ENABLED else {}


def _save() -> None:
    path = config.SLANG_FILE
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(_entries, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.warning("梗词典 %s 写入失败：%s", path, e)


def _norm(term: str) -> str:
    return " ".join((term or "").split()).strip("「」“”\"'`《》 ")


def _one_line(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _key(scope: str, term: str) -> str:
    return f"{scope}|{term.lower()}"


def learn(term: str, meaning: str, chat_id: int | None, scope: str = "chat",
          context: str = "", example: str = "", by: str = "") -> str | None:
    """给一个说法记下一种用法。成功返回规范化后的词，参数不合格返回 None。
    同一个词已有的用法不覆盖：新用法排到最前面，释义相同的旧用法被替换，超出 MAX_SENSES 丢最旧的。"""
    if not config.SLANG_ENABLED:
        return None
    term = _norm(term)
    meaning = _one_line(meaning, MEANING_CHARS)
    if not (MIN_TERM_CHARS <= len(term) <= MAX_TERM_CHARS) or not meaning:
        return None
    owner = GLOBAL if scope == GLOBAL or chat_id is None else str(chat_id)
    now = int(time.time())
    entry = _entries.setdefault(_key(owner, term), {"term": term, "scope": owner, "senses": []})
    sense = {"meaning": meaning, "context": _one_line(context, CONTEXT_CHARS),
             "example": _one_line(example, EXAMPLE_CHARS), "by": by, "t": now}
    entry["senses"] = [sense] + [s for s in entry["senses"] if s["meaning"] != meaning][: MAX_SENSES - 1]
    entry["term"], entry["used"] = term, now
    while len(_entries) > config.SLANG_MAX_ENTRIES:
        stale = min(_entries, key=lambda k: _entries[k].get("used", 0))
        del _entries[stale]
    _save()
    logger.info("记住说法 [%s] %s：%s（语境：%s）", owner, term, meaning, sense["context"] or "-")
    return term


def forget(term: str, chat_id: int) -> bool:
    """删掉本群专属和通用词典里的这个词（所有用法）。"""
    term = _norm(term)
    removed = [k for k in (_key(str(chat_id), term), _key(GLOBAL, term)) if _entries.pop(k, None)]
    if removed:
        _save()
    return bool(removed)


def _visible(chat_id: int) -> list[dict]:
    """本聊天能用的词：同一个词的本群用法排在通用用法前面，合计最多 MAX_SENSES 种。"""
    merged: dict[str, dict] = {}
    for scope in (str(chat_id), GLOBAL):
        for e in _entries.values():
            if e["scope"] != scope:
                continue
            view = merged.setdefault(e["term"].lower(), {"term": e["term"], "senses": [], "_src": []})
            view["senses"] = (view["senses"] + e["senses"])[:MAX_SENSES]
            view["_src"].append(e)
    return list(merged.values())


def _mentions(term: str, text: str) -> bool:
    """文本里是否出现了这个词。单字词只在短句里算数（「太典了」算，「字典里查不到」不算）。"""
    if len(term) > 1:
        return term in text
    return any(term in line and len("".join(line.split())) <= SHORT_LINE_CHARS for line in text.splitlines())


def lookup(text: str, chat_id: int) -> list[dict]:
    """找出文本里出现了的词；长词优先（「躺平摆烂」先于「躺平」），最多 INJECT_LIMIT 个。"""
    if not config.SLANG_ENABLED or not text:
        return []
    low = text.lower()
    hits = [e for e in _visible(chat_id) if _mentions(e["term"].lower(), low)]
    hits.sort(key=lambda e: len(e["term"]), reverse=True)
    picked: list[dict] = []
    for e in hits:
        # 已选的长词里包含了这个短词，就不重复附上
        if any(e["term"].lower() in p["term"].lower() for p in picked):
            continue
        picked.append(e)
        if len(picked) >= INJECT_LIMIT:
            break
    now = int(time.time())
    for view in picked:
        for e in view["_src"]:
            e["used"] = now  # 只在内存里刷新，下次写盘时一起保存；重启丢失只影响淘汰顺序
    return picked


def _sense_line(s: dict) -> str:
    extra = []
    if s.get("context"):
        extra.append(t("slang_when", context=s["context"]))
    if s.get("example"):
        extra.append(t("slang_example", example=s["example"]))
    if not extra:
        return t("slang_sense_plain", meaning=s["meaning"])
    return t("slang_sense", meaning=s["meaning"], extra=t("slang_sep").join(extra))


def describe(view: dict) -> str:
    """一个词的全部用法，给模型和 /slang 列表共用。"""
    lines = [t("slang_term", term=view["term"])]
    lines += [_sense_line(s) for s in view["senses"]]
    return "\n".join(lines)


def slang_block(text: str, chat_id: int) -> str:
    """注入给模型的参考附注；没有命中返回空串。"""
    hits = lookup(text, chat_id)
    if not hits:
        return ""
    return "[" + t("slang_header") + "\n" + "\n".join(describe(v) for v in hits) + "]"


def entries_for(chat_id: int) -> list[dict]:
    return sorted(_visible(chat_id), key=lambda e: e["term"].lower())


def run_remember(args: dict, chat_id: int | None) -> str:
    """remember_slang 工具入口：永不抛异常，返回给模型看的结果文本。"""
    scope = str(args.get("scope", "chat")).strip().lower()
    term = learn(str(args.get("term", "")), str(args.get("meaning", "")), chat_id,
                 scope=GLOBAL if scope == GLOBAL else "chat",
                 context=str(args.get("context", "")), example=str(args.get("example", "")))
    if term is None:
        return t("slang_bad_args", min=MIN_TERM_CHARS, max=MAX_TERM_CHARS)
    return t("slang_saved", term=term)
