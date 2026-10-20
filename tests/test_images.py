"""发图：send_image 工具按直链或关键词准备图片，正文定稿后跟着发出；下载时防内网、防假图。"""

import httpx
import pytest
from fakes import HIST, FakeMsg, chunk_text, chunk_tool, run, stream_of, use_model

from tgrok import chat, config, tools, viewer, web

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _two_rounds(args):
    rounds = []

    async def create(history, has_tools):
        rounds.append(list(history))
        if len(rounds) == 1:
            return stream_of([chunk_tool(0, "send_image", args)])
        return stream_of([chunk_text("这就是长城")])

    use_model(create)
    return rounds


def test_image_by_url_is_sent_after_the_reply(monkeypatch):
    async def fetch(url):
        return PNG, "image/png"

    monkeypatch.setattr(web, "fetch_image", fetch)
    rounds = _two_rounds({"url": "https://example.com/wall.png", "caption": "八达岭"})
    msg = FakeMsg()
    ids, answer = run(chat.stream_reply(msg, HIST))
    assert msg.sent[0].text == "这就是长城" and msg.media == [("photo", PNG, "八达岭")]
    assert "图片已准备好" in rounds[1][-1]["content"]
    assert answer.startswith("这就是长城") and "发了 1 张图：八达岭" in answer  # 追问时模型知道发过图


def test_image_by_query_picks_the_first_candidate_gemini_approves(monkeypatch):
    config.GEMINI_MEDIA_MODEL = "gemini-x"
    jpg = b"\xff\xd8\xff" + b"\x00" * 32

    async def search(query, n=8):
        return ["https://a.com/1.png", "https://b.com/2.jpg"]

    async def fetch(url):
        return (PNG, "image/png") if "1.png" in url else (jpg, "image/jpeg")

    async def check(data, mime, question):
        assert "长城" in question
        return data != PNG  # 第一张被判为不对题，第二张对题

    monkeypatch.setattr(web, "search_images", search)
    monkeypatch.setattr(web, "fetch_image", fetch)
    monkeypatch.setattr(viewer, "check", check)
    _two_rounds({"query": "长城"})
    msg = FakeMsg()
    run(chat.stream_reply(msg, HIST))
    assert msg.media == [("photo", jpg, None)]


def test_fake_image_and_nothing_found_are_reported_to_the_model(monkeypatch):
    async def html_as_image(url):
        return b"<html>hotlink denied</html>", "image/jpeg"

    async def no_results(query, n=8):
        return []

    monkeypatch.setattr(web, "fetch_image", html_as_image)
    monkeypatch.setattr(web, "search_images", no_results)
    send = tools.REGISTRY["send_image"]

    async def scenario():
        from tgrok.tools.send_image import outbox
        outbox.set([])
        return (await send.run({"url": "https://a.com/x.jpg"}, None), await send.run({"query": "长城"}, None),
                await send.run({}, None))

    fake, missing, empty = run(scenario())
    assert not fake.ok and "不是图片" in fake.content
    assert not missing.ok and "没找到" in missing.content
    assert not empty.ok
    assert not run(send.run({"url": "https://a.com/x.jpg"}, None)).ok  # 不在回复里：发不了


def test_fetch_image_refuses_redirects_into_the_private_network(monkeypatch):
    def handler(request):
        if request.url.host == "evil.com":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin.png"})
        if request.url.path == "/page":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>")
        if request.url.path == "/big.png":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"x" * (web.IMAGE_MAX_BYTES + 1))
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    monkeypatch.setattr(web, "_http", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert run(web.fetch_image("https://ok.com/a.png")) == (PNG, "image/png")
    for url in ("https://evil.com/a.png", "http://10.0.0.1/a.png", "https://ok.com/page", "https://ok.com/big.png"):
        with pytest.raises(web.WebError):
            run(web.fetch_image(url))


def test_page_images_lists_the_main_image_and_skips_icons():
    page = ('<meta property="og:image" content="/cover.jpg">'
            '<img src="https://cdn.x.com/logo.png"><img data-src="//cdn.x.com/photo.webp" src="data:,">'
            '<img src="/static/icon-32.png"><img src="http://192.168.1.2/a.jpg">')
    assert web._page_images(page, "https://news.x.com/a/1") == [
        "https://news.x.com/cover.jpg", "https://cdn.x.com/photo.webp"]
