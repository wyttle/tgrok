"""模型回复 → Telegram 富文本消息（sendRichMessage 的 html 字段）。

模型按约定输出「空行分段 + 白名单 HTML 标签」。这里负责：
- 转义：正文里的 < > & 一律当普通字符；只放行白名单标签 b i u s code pre a blockquote（含 expandable）tg-spoiler，
  其它标签（包括写错属性的 a）原样当文字显示。已经写成实体的 &lt; &amp; 等照常识别。
- 结构：富文本的 html 像网页一样会把换行压成空格，所以空行分出的每段包成 <p>，段内换行变成 <br>；
  <pre> 原样保留换行；可折叠引用只能装行内内容，里面的代码块改成行内代码。
- 配平：没闭合的标签在末尾补上，多余的闭合标签当文字；代码里的标签一律当文字；引用不嵌套。
- 兜底：模型偶尔还是写 Markdown（``` 代码块、**粗体**、`代码`、# 标题），顺手转成对应标签。
- 折叠：定稿时较长的回复如果没自己折叠，第一段之后的细节收进 <blockquote expandable>。
- 拆分：cut() 在顶层段落边界切开超长正文，切不开时在行边界切并补齐/重开标签，不会把标签切断。
"""

import html
import re
from dataclasses import dataclass, field

INLINE = {"b", "i", "u", "s", "code", "a", "tg-spoiler"}
BLOCK = {"pre", "blockquote"}
ALLOWED = INLINE | BLOCK
FOLD_CHARS = 700  # 定稿时正文超过这么多字、段落够多又没自己折叠，就把细节收起来
FOLD_MIN_PARAGRAPHS = 3

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*?)?)\s*(/?)>")
_ENTITY_RE = re.compile(r"&(?:lt|gt|amp|quot|#\d{1,7}|#x[0-9a-fA-F]{1,6});")
_HREF_RE = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.I)
_LANG_RE = re.compile(r"""\bclass\s*=\s*["']?language-([\w+#.-]{1,30})""", re.I)
_FENCE_RE = re.compile(r"```[ \t]*([\w+#.-]*)[ \t]*\n(.*?)(?:```|\Z)", re.S)
_MD_INLINE_RE = re.compile(r"\*\*([^*\n]+?)\*\*|`([^`\n]+)`")
_MD_HEADING_RE = re.compile(r"^([ \t]*[^\w#\n<]*?)[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_BLANK_RE = re.compile(r"\n[ \t]*\n+")


@dataclass
class Node:
    name: str  # "" = 根
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)  # Node 或 str（已解码的纯文字）


def _decode(raw: str) -> str:
    """只解码合法实体，其余 & 原样当文字。"""
    return _ENTITY_RE.sub(lambda m: html.unescape(m.group(0)), raw)


def _fences_to_tags(text: str) -> str:
    """``` 代码块 → <pre><code>（内容转义后放进去，后面的解析不会把里面的 < 当标签）。"""
    def repl(m: re.Match) -> str:
        lang = f' class="language-{m.group(1)}"' if m.group(1) else ""
        body = html.escape(m.group(2).rstrip("\n"), quote=False)
        return f"<pre><code{lang}>{body}</code></pre>"
    return _FENCE_RE.sub(repl, text)


def _in(stack: list[Node], *names: str) -> bool:
    return any(n.name in names for n in stack)


def _add_text(parent: Node, text: str, raw_md: bool) -> None:
    """往节点里加文字；不在代码里时把漏网的 Markdown（**粗体**、`代码`、# 标题）转成节点。"""
    if not text:
        return
    if not raw_md:
        parent.children.append(text)
        return
    # 标题行（Grok 有时在 # 前加个符号）改成加粗；符号留在前面
    text = _MD_HEADING_RE.sub(lambda m: f"{m.group(1)}\x00b{m.group(2)}\x00/b", text)
    pos = 0
    for m in _MD_INLINE_RE.finditer(text):
        _add_plain_with_headings(parent, text[pos:m.start()])
        if m.group(1) is not None:
            parent.children.append(Node("b", children=[m.group(1)]))
        else:
            parent.children.append(Node("code", children=[m.group(2)]))
        pos = m.end()
    _add_plain_with_headings(parent, text[pos:])


def _add_plain_with_headings(parent: Node, text: str) -> None:
    for i, part in enumerate(re.split(r"\x00b(.*?)\x00/b", text)):
        if not part:
            continue
        parent.children.append(Node("b", children=[part]) if i % 2 else part)


_DROP = object()  # 白名单标签但放的位置不合法（引用套引用、同名嵌套、块放进行内格式）：去掉标签、保留内容


def parse(text: str) -> Node:
    """模型输出 → 只含白名单标签、配平好的树。"""
    text = _fences_to_tags(text or "")
    root = Node("")
    stack = [root]
    dropped: dict[str, int] = {}  # 被去掉的开标签数，它们对应的闭合标签也一并去掉
    pos = 0
    for m in _TAG_RE.finditer(text):
        top = stack[-1]
        literal_ctx = _in(stack, "code", "pre")
        _add_text(top, _decode(text[pos:m.start()]), raw_md=not literal_ctx)
        pos = m.end()
        closing, name, attrs, selfclose = m.group(1) == "/", m.group(2).lower(), m.group(3) or "", m.group(4)
        raw = m.group(0)
        if name not in ALLOWED or selfclose:
            top.children.append(raw)
            continue
        if closing:
            if dropped.get(name) and not literal_ctx:
                dropped[name] -= 1
                continue
            idx = next((i for i in range(len(stack) - 1, 0, -1) if stack[i].name == name), None)
            # 代码里只认它自己的闭合标签；对不上的闭合标签当文字
            if idx is None or (literal_ctx and stack[-1].name != name):
                top.children.append(raw)
            else:
                del stack[idx:]
            continue
        node = _open(name, attrs, stack)
        if node is _DROP:
            dropped[name] = dropped.get(name, 0) + 1
            continue
        if node is None:
            top.children.append(raw)
            continue
        top.children.append(node)
        stack.append(node)
    _add_text(stack[-1], _decode(text[pos:]), raw_md=not _in(stack, "code", "pre"))
    return root


def _open(name: str, attrs: str, stack: list[Node]):
    """校验一个开标签，返回新节点；None = 当文字显示（代码里的标签、a 没有 http(s) 链接）；
    _DROP = 去掉这个标签但保留内容（放的位置 Telegram 不支持）。"""
    if _in(stack, "code", "pre"):
        # 代码里只允许 <pre> 紧跟一个 <code class="language-x">
        if name == "code" and stack[-1].name == "pre" and not stack[-1].children:
            lang = _LANG_RE.search(attrs)
            return Node("code", {"lang": lang.group(1)} if lang else {})
        return None
    if name in BLOCK and any(n.name in INLINE for n in stack):
        return _DROP  # 块不能放进行内格式里
    if name == "blockquote":
        if _in(stack, "blockquote"):
            return _DROP
        return Node("blockquote", {"expandable": True} if re.search(r"\bexpandable\b", attrs, re.I) else {})
    if name == "a":
        m = _HREF_RE.search(attrs)
        href = html.unescape(next((g for g in (m.groups() if m else ()) if g), "")).strip()
        if not re.match(r"https?://\S+$", href, re.I):
            return None
        if _in(stack, "a"):
            return _DROP
        return Node("a", {"href": href})
    if name in {n.name for n in stack}:
        return _DROP  # 同名行内标签不嵌套
    return Node(name)


# ---- 输出 ----

def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _flat(node: Node) -> str:
    return "".join(c if isinstance(c, str) else _flat(c) for c in node.children)


def _inline(children: list, br: bool = True) -> str:
    """行内内容 → html；br=True 时换行变 <br>。"""
    out = []
    for c in children:
        if isinstance(c, str):
            out.append(_esc(c).replace("\n", "<br>") if br else _esc(c))
        elif c.name == "pre":  # 只会出现在可折叠引用里：改成行内代码
            out.append("<code>" + _esc(_flat(c).strip("\n")).replace("\n", "<br>") + "</code>")
        elif c.name == "a":
            out.append(f'<a href="{html.escape(c.attrs["href"])}">' + _inline(c.children, br) + "</a>")
        elif c.name == "code":
            out.append("<code>" + _esc(_flat(c)).replace("\n", "<br>") + "</code>")
        else:
            out.append(f"<{c.name}>" + _inline(c.children, br) + f"</{c.name}>")
    return "".join(out)


def _paragraphs(children: list) -> list[list]:
    """一串行内内容按顶层文字里的空行切成段落（每段是一串子节点）。"""
    paras, cur = [], []
    for c in children:
        if isinstance(c, str):
            pieces = _BLANK_RE.split(c)
            for i, piece in enumerate(pieces):
                if i:
                    paras.append(cur)
                    cur = []
                if piece:
                    cur.append(piece)
        else:
            cur.append(c)
    paras.append(cur)
    out = []
    for p in paras:
        # 去掉段首段尾的换行和空白
        while p and isinstance(p[0], str) and not p[0].strip():
            p = p[1:]
        while p and isinstance(p[-1], str) and not p[-1].strip():
            p = p[:-1]
        if p:
            if isinstance(p[0], str):
                p = [p[0].lstrip()] + p[1:]
            if isinstance(p[-1], str):
                p = p[:-1] + [p[-1].rstrip()]
            out.append(p)
    return out


def _blocks(children: list) -> list[tuple[str, object]]:
    """子节点 → 块序列：("p", 行内节点列表) / ("pre", Node) / ("quote", Node)。"""
    blocks, run = [], []

    def flush():
        blocks.extend(("p", p) for p in _paragraphs(run))
        run.clear()

    for c in children:
        if isinstance(c, Node) and c.name in BLOCK:
            flush()
            blocks.append(("pre" if c.name == "pre" else "quote", c))
        else:
            run.append(c)
    flush()
    return blocks


def _pre_html(node: Node) -> str:
    code = next((c for c in node.children if isinstance(c, Node) and c.name == "code"), None)
    body = _esc(_flat(node).strip("\n"))
    if code is not None and code.attrs.get("lang"):
        return f'<pre><code class="language-{code.attrs["lang"]}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def _blocks_html(blocks: list) -> str:
    out = []
    for kind, item in blocks:
        if kind == "p":
            out.append("<p>" + _inline(item) + "</p>")
        elif kind == "pre":
            out.append(_pre_html(item))
        elif item.attrs.get("expandable"):
            # 可折叠引用只能装行内内容：段落之间用空行（<br><br>）隔开
            paras = _paragraphs(item.children)
            inner = "<br><br>".join(_inline(p) for p in paras)
            if inner:
                out.append("<blockquote expandable>" + inner + "</blockquote>")
        else:
            inner = _blocks_html(_blocks(item.children))
            if inner:
                out.append("<blockquote>" + inner + "</blockquote>")
    return "".join(out)


def _plain_len(blocks: list) -> int:
    return sum(len(_flat(Node("", children=item)) if kind == "p" else _flat(item)) for kind, item in blocks)


def _is_links_paragraph(item: list) -> bool:
    text = _flat(Node("", children=item))
    return len(text) <= 200 and (any(isinstance(c, Node) and c.name == "a" for c in item) or "http" in text)


def _fold(blocks: list) -> list:
    """较长、段落多、没有代码块也没有自己写引用的回复：第一段留在外面，其余收进可折叠引用；
    末尾那段如果只是链接就留在外面。"""
    paras = [b for b in blocks if b[0] == "p"]
    if (len(paras) != len(blocks) or len(paras) < FOLD_MIN_PARAGRAPHS
            or _plain_len(blocks) < FOLD_CHARS):
        return blocks
    head, rest = blocks[:1], blocks[1:]
    tail = [rest.pop()] if rest and _is_links_paragraph(rest[-1][1]) else []
    if not rest:
        return blocks
    folded = Node("blockquote", {"expandable": True})
    for i, (_, item) in enumerate(rest):
        if i:
            folded.children.append("\n\n")
        folded.children.extend(item)
    return head + [("quote", folded)] + tail


def to_rich_html(text: str, cursor: str = "", fold: bool = False) -> str:
    """模型输出 → sendRichMessage 的 html。cursor 是流式输出时挂在末尾的光标文字；fold 只在定稿时开。"""
    root = parse(text)
    if cursor:
        root.children.append(cursor)
    blocks = _blocks(root.children)
    if fold:
        blocks = _fold(blocks)
    return _blocks_html(blocks) or "<p>" + _esc(cursor or " ") + "</p>"


def to_plain(text: str) -> str:
    """模型输出 → 纯文本（富文本发送失败时的回退、进度行、存进记忆和最近群聊）：去掉标签，
    链接写成「文字（网址）」，段落之间空一行。"""
    def walk(children: list) -> str:
        out = []
        for c in children:
            if isinstance(c, str):
                out.append(c)
            elif c.name == "a":
                label = walk(c.children)
                out.append(label if label.strip() == c.attrs["href"] else f"{label}（{c.attrs['href']}）")
            elif c.name in BLOCK:
                out.append("\n\n" + walk(c.children).strip("\n") + "\n\n")
            else:
                out.append(walk(c.children))
        return "".join(out)
    return re.sub(r"\n{3,}", "\n\n", walk(parse(text).children)).strip()


def plain_to_html(text: str) -> str:
    """bot 自己的纯文本（命令输出、提示）→ 富文本：全部转义，空行分段，换行保留。"""
    paras = [p.strip("\n") for p in _BLANK_RE.split(text or "") if p.strip()]
    return "".join("<p>" + _esc(p).replace("\n", "<br>") + "</p>" for p in paras) or "<p> </p>"


# ---- 拆分 ----

def _open_stack_at(text: str, end: int) -> list[str]:
    """text[:end] 结束时还开着的白名单标签（原样的开标签字符串），用来在切点补齐/重开。"""
    stack: list[tuple[str, str]] = []
    pre_text = _fences_to_tags(text[:end])
    for m in _TAG_RE.finditer(pre_text):
        name = m.group(2).lower()
        if name not in ALLOWED or m.group(4):
            continue
        if m.group(1):
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == name:
                    del stack[i:]
                    break
        else:
            stack.append((name, m.group(0)))
    return [raw for _, raw in stack]


def _closers(opened: list[str]) -> str:
    return "".join(f"</{_TAG_RE.match(o).group(2).lower()}>" for o in reversed(opened))


def cut(text: str, limit: int) -> tuple[str, str]:
    """把超过 limit 的正文切成（前段, 剩余）。优先切在没有开着标签的空行处；其次任意空行或换行，
    这时前段末尾补上闭合标签、剩余开头重开这些标签——标签永远不会被切断。"""
    window = text[:limit]
    candidates = [m.start() for m in re.finditer(r"\n[ \t]*\n", window) if m.start() > limit // 3]
    for pos in reversed(candidates):
        if not _open_stack_at(text, pos) and not _unclosed_fence(text[:pos]):
            return text[:pos].rstrip(), text[pos:].lstrip("\n")
    pos = window.rfind("\n", limit // 3)
    if pos == -1:
        pos = window.rfind(" ", limit // 3)
    if pos == -1:
        pos = limit
    while pos > 0 and text[:pos].rfind("<") > text[:pos].rfind(">"):
        pos = text[:pos].rfind("<")  # 不切在标签中间
    if _unclosed_fence(text[:pos]):
        head, tail = text[:pos].rstrip("\n") + "\n```", "```\n" + text[pos:].lstrip("\n")
        return head, tail
    opened = _open_stack_at(text, pos)
    return text[:pos].rstrip() + _closers(opened), "".join(opened) + text[pos:].lstrip("\n")


def _unclosed_fence(text: str) -> str:
    return "```" if text.count("```") % 2 else ""
