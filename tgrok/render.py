"""回复正文的显示格式：Markdown 转 Telegram MarkdownV2、流式中间态、压成一整段。"""

import re

import telegramify_markdown

from . import config

# 匹配标题行：行首可选的符号前缀 + 1~6 个 #。Grok 有时输出带符号前缀的标题，
# 此时 # 不在行首，telegramify/CommonMark 不认作标题，会把 ## 原样
# 泄漏成 \#\#。这里去掉前缀符号、让 # 回到行首，使下游正常渲染成加粗。
_HEADING_RE = re.compile(r"^[ \t]*[^\w#\n]*[ \t]*(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_MD_SPECIAL_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def to_telegram_markdown(text: str) -> str:
    """模型返回的标准 Markdown → Telegram MarkdownV2（先把标题行的 # 挪回行首）。"""
    return telegramify_markdown.markdownify(_HEADING_RE.sub(r"\1 \2", text))


def streaming_markdown(text: str) -> str:
    """流式中间态的 MarkdownV2，末尾带光标：未闭合的强调会被转义、未闭合的代码块会被补齐，
    所以半截正文也能安全解析，定稿时不会从原始 ** 突然跳变成格式化文本。"""
    md = to_telegram_markdown(text).rstrip("\n")
    cursor = _MD_SPECIAL_RE.sub(r"\\\1", config.STREAM_CURSOR)
    # 光标不能挂在代码块收尾的 ``` 同一行
    return md + ("\n" if md.endswith("```") else "") + cursor


_LIST_MARK_RE = re.compile(r"^\s*(?:[-*+•·]|\d+[.、)]|#{1,6})\s+")
_CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u9fff\uff00-\uffef]")
_END_PUNCT = "。！？.!?；;：:，,、…）)」』】》\"'"


def single_paragraph(text: str) -> str:
    """把模型输出压成一整段：去掉分点、编号和标题符号，按中英文习惯把各行接起来。
    代码块原样保留（里面的换行有意义）。"""
    parts = re.split(r"(```.*?(?:```|$))", text, flags=re.S)
    out = ""
    for i, part in enumerate(parts):
        if i % 2:  # 代码块
            out = out.rstrip() + ("\n" if out.strip() else "") + part.strip("\n") + "\n"
            continue
        for line in part.splitlines():
            line = _LIST_MARK_RE.sub("", line).strip()
            if not line:
                continue
            if not out or out.endswith("\n"):
                out += line
            elif out[-1] in _END_PUNCT:
                out += ("" if _CJK_RE.match(out[-1]) else " ") + line
            elif _CJK_RE.match(out[-1]):
                out += "，" + line  # 中文行没收尾就换行了（标题、分点），补个逗号接上
            else:
                out += ", " + line  # 英文分点同理
    return out.strip()


def shape(text: str) -> str:
    """按配置决定是否把回复压成一段。"""
    return single_paragraph(text) if config.REPLY_SINGLE_PARAGRAPH else text
