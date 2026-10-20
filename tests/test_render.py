"""回复的富文本格式：转义和白名单、段落结构、折叠、拆分不切断标签、富文本被拒时回退纯文本。"""

import re

from fakes import HIST, FakeMsg, _RichApi, chunk_text, run, stream_of, use_model

from tgrok import chat, render, rich


def _balanced(html: str) -> bool:
    names = re.findall(r"<(/?)(b|i|u|s|code|pre|a|blockquote|tg-spoiler|p)\b", html)
    depth: dict[str, int] = {}
    for closing, name in names:
        depth[name] = depth.get(name, 0) + (-1 if closing else 1)
        if depth[name] < 0:
            return False
    return all(v == 0 for v in depth.values())


def test_short_reply_is_one_plain_paragraph():
    assert render.to_rich_html("好的，明天八点。", fold=True) == "<p>好的，明天八点。</p>"


def test_paragraphs_and_line_breaks_survive():
    # 富文本 html 会把换行压成空格：空行分段要变 <p>，段内换行要变 <br>
    assert render.to_rich_html("第一段\n第二行\n\n第二段") == "<p>第一段<br>第二行</p><p>第二段</p>"


def test_code_block_keeps_newlines_and_is_escaped():
    out = render.to_rich_html('看：\n\n<pre><code class="language-python">if a < b:\n    print("x & y")</code></pre>')
    assert out == ('<p>看：</p><pre><code class="language-python">if a &lt; b:\n    print("x &amp; y")'
                   '</code></pre>')
    # 模型漏写成 Markdown 代码块时同样渲染成代码块
    assert render.to_rich_html("```js\nx < 1\n```") == '<pre><code class="language-js">x &lt; 1</code></pre>'


def test_comparison_operators_are_escaped_not_parsed():
    assert render.to_rich_html("a < b && c > d") == "<p>a &lt; b &amp;&amp; c &gt; d</p>"
    assert render.to_plain("a < b && c > d") == "a < b && c > d"


def test_only_whitelisted_tags_pass_others_show_as_text():
    out = render.to_rich_html('<b>粗</b> <script>x</script> <a href="javascript:1">坏</a> <a href="https://t.me">好</a>')
    assert out == ('<p><b>粗</b> &lt;script&gt;x&lt;/script&gt; &lt;a href="javascript:1"&gt;坏&lt;/a&gt; '
                   '<a href="https://t.me">好</a></p>')


def test_broken_markup_is_repaired():
    assert render.to_rich_html("<b>没闭合 <i>斜") == "<p><b>没闭合 <i>斜</i></b></p>"
    assert render.to_rich_html("多余</b>闭合") == "<p>多余&lt;/b&gt;闭合</p>"
    # 引用不能嵌套：里层标签去掉，内容留下
    assert render.to_rich_html("<blockquote>外<blockquote>内</blockquote></blockquote>") == \
        "<blockquote><p>外内</p></blockquote>"
    # 可折叠引用只能装行内内容：里面的代码块改成行内代码
    assert render.to_rich_html("<blockquote expandable>说明\n<pre>a\nb</pre></blockquote>") == \
        "<blockquote expandable>说明<br><code>a<br>b</code></blockquote>"


def test_long_reply_folds_details_but_keeps_conclusion_and_links_outside():
    details = "\n\n".join(f"第{i}点：" + "细节说明" * 60 for i in range(3))
    text = f"结论：能用。\n\n{details}\n\n<a href=\"https://example.com\">来源</a>"
    out = render.to_rich_html(text, fold=True)
    assert out.startswith("<p>结论：能用。</p><blockquote expandable>第0点：")
    assert "<br><br>第1点：" in out and out.endswith('</blockquote><p><a href="https://example.com">来源</a></p>')
    # 流式过程中不折叠；自己写了引用或有代码块的不再自动折叠
    assert "expandable" not in render.to_rich_html(text)
    assert render.to_rich_html("<blockquote>引</blockquote>\n\n" + details, fold=True).count("<blockquote") == 1


def test_cut_never_splits_a_tag_and_each_piece_renders():
    text = "\n\n".join(f"<b>第{i}段</b> " + "字" * 300 for i in range(60))
    pieces, rest = [], text
    while len(rest) > 4000:
        head, rest = render.cut(rest, 4000)
        pieces.append(head)
    pieces.append(rest)
    assert len(pieces) > 1 and all(len(p) <= 4000 for p in pieces)
    assert all(_balanced(render.to_rich_html(p)) for p in pieces)
    assert "".join(render.to_plain(p) for p in pieces).replace("\n", "") == render.to_plain(text).replace("\n", "")
    # 一整块代码也切得开：前段补上 </pre>，后段重开 <pre>
    code = "<pre>" + "\n".join(f"line{i}" for i in range(2000)) + "</pre>"
    head, tail = render.cut(code, 4000)
    assert head.endswith("</pre>") and tail.startswith("<pre>") and len(head) <= 4010
    assert render.to_rich_html(tail).startswith("<pre>line")


def test_rejected_rich_text_falls_back_to_plain_and_splits(monkeypatch):
    monkeypatch.setattr(_RichApi, "reject", True)
    msg = FakeMsg()
    long_text = "\n\n".join("段落" * 500 for _ in range(5))
    sent = run(rich.send_rich(msg, render.to_rich_html(long_text), render.to_plain(long_text)))
    assert len(sent) == 2 and all(len(s.text) <= rich.PLAIN_CHARS for s in sent)
    assert "".join(s.text for s in sent).replace("\n", "") == long_text.replace("\n", "")


def test_reply_is_sent_as_rich_text_end_to_end():
    async def create(history, has_tools):
        return stream_of([chunk_text("结论一句。\n\n"), chunk_text('<pre><code class="language-sh">ls -l</code></pre>')])

    use_model(create)
    msg = FakeMsg()
    _, answer = run(chat.stream_reply(msg, HIST))
    assert msg.sent[0].html == '<p>结论一句。</p><pre><code class="language-sh">ls -l</code></pre>'
    assert answer == '结论一句。\n\n<pre><code class="language-sh">ls -l</code></pre>'  # 历史里留模型原文


def test_bad_markup_reply_still_reaches_the_chat_as_plain_text(monkeypatch):
    """Telegram 拒绝富文本时，回复改成纯文本发出，不会被吞掉。"""
    monkeypatch.setattr(_RichApi, "reject", True)

    async def create(history, has_tools):
        return stream_of([chunk_text("<b>重点</b>：a < b")])

    use_model(create)
    msg = FakeMsg()
    _, answer = run(chat.stream_reply(msg, HIST))
    assert answer and msg.sent[0].text == "重点：a < b"
