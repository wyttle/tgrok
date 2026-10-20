"""回复的显示格式。"""

from tgrok import render


def test_single_paragraph_joins_lines_like_a_native_writer():
    # 中文行之间不加空格，没收尾的行补逗号；英文分点用逗号接上
    assert render.single_paragraph("## 结论\n能。\n\n步骤：\n1. 先烧水\n2. 放面") == "结论，能。步骤：先烧水，放面"
    assert render.single_paragraph("Short answer: yes.\n\n- one\n- two") == "Short answer: yes. one, two"


def test_single_paragraph_keeps_code_blocks_intact():
    out = render.single_paragraph("看代码：\n\n```python\nprint(1)\nprint(2)\n```\n\n就这。")
    assert "```python\nprint(1)\nprint(2)\n```" in out and "\n\n" not in out


def test_symbol_prefixed_headings_render_as_headings():
    # Grok 有时在 # 前加符号，不处理会把 ## 原样泄漏成 \#\#
    assert "\\#" not in render.to_telegram_markdown("🔥 ## 结论\n正文")
