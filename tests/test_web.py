"""联网层：只读公网地址、HTML 正文提取。"""

from tgrok import web


def test_only_public_http_urls_are_fetched():
    assert web._is_public_http_url("https://a.com/x")
    assert not web._is_public_http_url("http://127.0.0.1/x")
    assert not web._is_public_http_url("http://localhost/x")
    assert not web._is_public_http_url("file:///etc/passwd")


def test_html_text_extraction_prefers_main_content():
    title, text = web._html_to_text("<html><head><title>T</title></head><body><main><p>Hi</p></main></body></html>")
    assert title == "T" and "Hi" in text
