"""联网层：搜索源聚合、Gemini grounding、网页抓取与正文提取。"""

import asyncio
import html
import ipaddress
import logging
import multiprocessing
import os
import re
import time
from urllib.parse import urljoin, urlparse

import httpx

from . import config, llm, logs, usage
from .i18n import t

logger = logging.getLogger(__name__)
_grounding_cooldown_until = 0.0


class WebError(Exception):
    """搜索或读网页没拿到内容；str(e) 是给模型看的说明（为什么失败、接下来怎么办）。"""


# 共享 HTTP 连接池：搜索/抓取复用连接，省掉每次请求的 TLS 握手。
# 进程常驻，不显式关闭；超时在每次请求上单独指定。
_http = httpx.AsyncClient(follow_redirects=True)

# 在父进程启动时（单线程阶段）预热导入：fork 出的提取子进程不再走导入机制。
# 运行期 fork 时若其他线程恰好持有 import 锁，子进程内再 import 会永久死锁。
try:
    import trafilatura
except ImportError:
    trafilatura = None

async def _search_tavily(query: str) -> list[dict]:
    resp = await _http.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}"},
        json={"query": query, "max_results": config.SEARCH_MAX_RESULTS, "search_depth": "basic"},
        timeout=config.SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in data.get("results", [])
    ]


async def _search_searxng(query: str) -> list[dict]:
    resp = await _http.get(
        f"{config.SEARXNG_BASE_URL}/search", params={"q": query, "format": "json"},
        timeout=config.SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in data.get("results", [])[:config.SEARCH_MAX_RESULTS]
    ]


async def _search_duckduckgo(query: str) -> list[dict]:
    from ddgs import DDGS  # 惰性导入：仅 duckduckgo 源需要安装 ddgs

    def _run() -> list[dict]:
        with DDGS(timeout=config.SEARCH_TIMEOUT) as ddgs:
            return list(ddgs.text(query, max_results=config.SEARCH_MAX_RESULTS))

    rows = await asyncio.to_thread(_run)
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("href") or r.get("url", ""),
            "snippet": r.get("body") or r.get("description", ""),
        }
        for r in rows
    ]


def format_search_results(query: str, rows: list[dict]) -> str:
    blocks, total = [], 0
    for i, r in enumerate(rows, 1):
        snippet = (r.get("snippet") or "").strip()[:config.SEARCH_SNIPPET_LIMIT]
        block = f"[{i}] {r.get('title', '')}\n{r.get('url', '')}\n{snippet}"
        if total + len(block) > config.SEARCH_RESULT_CHAR_LIMIT:
            break
        blocks.append(block)
        total += len(block)
    if not blocks:
        raise WebError(t("search_no_results", query=query))
    return "\n\n".join(blocks)


async def _search_serper(query: str) -> list[dict]:
    """Serper.dev：真实 Google 搜索结果（含答案框）。"""
    resp = await _http.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
        json={"q": query, "num": config.SEARCH_MAX_RESULTS},
        timeout=config.SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    rows = []
    box = data.get("answerBox") or {}
    if box.get("answer") or box.get("snippet"):
        rows.append({
            "title": box.get("title", "Answer"),
            "url": box.get("link", ""),
            "snippet": box.get("answer") or box.get("snippet", ""),
        })
    rows += [
        {"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", "")}
        for r in (data.get("organic") or [])[:config.SEARCH_MAX_RESULTS]
    ]
    return rows


_PROVIDER_SEARCH = {
    "tavily": _search_tavily,
    "serper": _search_serper,
    "searxng": _search_searxng,
    "duckduckgo": _search_duckduckgo,
}


async def _search_gemini_grounded(query: str) -> str | None:
    """混合模式：用 config.GEMINI_SEARCH_MODEL + google_search grounding 执行检索。

    返回带来源链接的事实综述文本；失败返回 None（调用方回退普通搜索源），
    429 配额超限进入冷却期。
    """
    global _grounding_cooldown_until
    from .llm import gemini as llm_gemini

    if time.monotonic() < _grounding_cooldown_until:
        return None
    tools = [llm_gemini.gtypes.Tool(google_search=llm_gemini.gtypes.GoogleSearch())]
    try:
        tools.append(llm_gemini.gtypes.Tool(url_context=llm_gemini.gtypes.UrlContext()))
    except AttributeError:
        pass
    logger.info("Gemini grounding 检索 (%s): %s", config.GEMINI_SEARCH_MODEL, logs.one_line(query, 120))
    t0 = time.monotonic()
    try:
        resp = await llm_gemini.gemini_client.aio.models.generate_content(
            model=config.GEMINI_SEARCH_MODEL,
            contents=query,
            config=llm_gemini.gtypes.GenerateContentConfig(
                system_instruction=(
                    "你是检索助手。用搜索工具核实查询内容，简洁列出相关的最新事实要点，"
                    "逐条尽量注明日期；只给事实，不要评论。用查询本身的语言回答。"
                ),
                max_output_tokens=2048,
                tools=tools,
            ),
        )
    except Exception as e:
        usage.record("search", config.GEMINI_SEARCH_MODEL, 0, 0, time.monotonic() - t0, ok=False)
        if llm.is_quota_error(e):
            _grounding_cooldown_until = time.monotonic() + config.GEMINI_SEARCH_COOLDOWN
            logger.warning(
                "Gemini grounding 配额超限（429），%.0f 分钟内回退自带搜索源",
                config.GEMINI_SEARCH_COOLDOWN / 60,
            )
        else:
            logger.warning("Gemini grounding 检索失败：%s: %s", type(e).__name__, str(e)[:200])
        return None
    spent = llm_gemini.usage_of(resp)
    usage.record("search", config.GEMINI_SEARCH_MODEL, spent.input, spent.output, time.monotonic() - t0)
    text = (getattr(resp, "text", None) or "").strip()
    if not text:
        return None
    seen, links = set(), []
    for cand in getattr(resp, "candidates", None) or []:
        gm = getattr(cand, "grounding_metadata", None)
        for gc in getattr(gm, "grounding_chunks", None) or []:
            web = getattr(gc, "web", None)
            uri = getattr(web, "uri", None)
            if uri and uri not in seen:
                seen.add(uri)
                links.append(f"[{getattr(web, 'title', '') or uri}]({uri})")
    if links:
        text += "\n\n" + t("sources") + "\n" + "\n".join(links[:5])
    return text[: config.SEARCH_RESULT_CHAR_LIMIT + 800]


async def run_web_search(query: str) -> str:
    """并发聚合所有已配置的搜索源，返回编号的结果列表；搜不到或全部失败时抛 WebError。

    配置了 config.GEMINI_SEARCH_MODEL 时优先用 Gemini grounding（Google 服务端检索），
    失败/冷却期自动回退到普通搜索源。
    """
    query = (query or "").strip()
    if not query:
        raise WebError(t("search_no_results", query=""))
    if config.GEMINI_SEARCH_MODEL:
        grounded = await _search_gemini_grounded(query)
        if grounded is not None:
            return grounded
    providers = config.ACTIVE_PROVIDERS
    logger.info("联网搜索 (%s): %s", "+".join(providers), query)
    outcomes = await asyncio.gather(
        *(_PROVIDER_SEARCH[p](query) for p in providers), return_exceptions=True
    )
    grouped, first_error = [], None
    for provider, outcome in zip(providers, outcomes):
        if isinstance(outcome, BaseException):
            first_error = first_error or outcome
            logger.warning("搜索源 %s 失败：%s: %s", provider, type(outcome).__name__, outcome)
        elif outcome:
            grouped.append(outcome)
    if not grouped:
        if first_error is not None:
            raise WebError(t("search_error", error=type(first_error).__name__))
        raise WebError(t("search_no_results", query=query))
    # 各源结果交错合并（每源轮流出一条）并按 URL 去重，保证每个源都有机会排前
    rows, seen = [], set()
    for tier in range(max(len(g) for g in grouped)):
        for g in grouped:
            if tier < len(g):
                r = g[tier]
                key = (r.get("url") or "").split("#")[0].rstrip("/") or r.get("title", "")
                if key not in seen:
                    seen.add(key)
                    rows.append(r)
    return format_search_results(query, rows)


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_HEAD_RE = re.compile(r"<head\b.*?</head\s*>", re.I | re.S)
_MAIN_RE = re.compile(r"<(main|article)\b.*?</\1\s*>", re.I | re.S)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript|svg)\b.*?</\1\s*>", re.I | re.S)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|br|li|ul|ol|tr|table|h[1-6]|section|article|header|footer|blockquote|pre)\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(page: str) -> tuple[str, str]:
    """极简 HTML 正文提取：去掉 script/style/标签，块级标签转换行。返回 (标题, 正文)。"""
    m = _TITLE_RE.search(page)
    title = html.unescape(m.group(1)).strip() if m else ""
    page = _HEAD_RE.sub(" ", page)
    # 页面声明了 main/article 正文区域时只取该区域，省掉导航/页脚等噪音
    m = _MAIN_RE.search(page)
    if m and len(m.group(0)) > 1000:
        page = m.group(0)
    page = _SCRIPT_STYLE_RE.sub(" ", page)
    page = _HTML_COMMENT_RE.sub(" ", page)
    page = _BLOCK_TAG_RE.sub("\n", page)
    page = _TAG_RE.sub(" ", page)
    page = html.unescape(page)
    lines = (re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in page.split("\n"))
    return title, "\n".join(ln for ln in lines if ln)


def _is_public_http_url(url: str) -> bool:
    """只允许公网 http(s) 地址，拒绝内网/回环地址，避免模型探测内网（SSRF）。"""
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if parts.hostname == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return True  # 是域名而非 IP 字面量；DNS 解析级别的校验不在此处做
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)


def _extract_html_text(page: str) -> tuple[str, str]:
    """HTML → (标题, 正文)。优先 trafilatura 快速模式，失败回退内置极简提取。

    只用 fast 模式：trafilatura 的完整模式会在困难页面上级联 readability/justext
    等纯 Python 兜底算法，可能长时间占住 GIL；难提取的页面交给 Jina Reader 兜底。
    """
    m = _TITLE_RE.search(page)
    title = html.unescape(m.group(1)).strip() if m else ""
    text = ""
    if trafilatura is not None:
        for kwargs in (
            {"output_format": "markdown", "include_links": False, "include_tables": True, "fast": True},
            {"output_format": "markdown", "include_links": False, "include_tables": True, "no_fallback": True},
            {"no_fallback": True},
        ):
            try:
                text = trafilatura.extract(page, **kwargs) or ""
                break
            except (TypeError, ValueError):  # 参数名随版本变化（fast ↔ no_fallback）
                continue
    if not text.strip():
        _, text = _html_to_text(page)
    return title, text.strip()


def _extract_worker(page: str, conn) -> None:
    # fork 复制了父进程的锁状态：立刻禁用 logging，避免在可能已死锁的日志锁上挂起
    logging.disable(logging.CRITICAL)
    try:
        title, text = _extract_html_text(page)
        # 必须在子进程内截断：管道缓冲区只有 64KB，发大文本会写阻塞，
        # 而父进程在等子进程退出后才读 → 互等死锁（multiprocessing 经典陷阱）
        conn.send((title[:500], text[: config.FETCH_CHAR_LIMIT + 200]))
    except Exception:
        try:
            conn.send(("", ""))
        except Exception:
            pass
    finally:
        conn.close()


EXTRACT_TIMEOUT = 10.0  # 正文提取的看门狗秒数，超时强杀提取进程


def _extract_in_process(page: str, timeout: float) -> tuple[str, str] | None:
    """在独立进程里跑正文提取，超时 terminate。

    提取是 CPU 密集的纯 Python/lxml 工作，放线程里会长时间占住 GIL、饿死事件循环
    （LLM 流没人消费、Telegram 轮询停摆）；独立进程不共享 GIL 且可强杀。
    先 poll+recv 再收尸，避免「子进程写管道阻塞 vs 父进程 join 等退出」互等。
    本函数本身阻塞，调用方需套 asyncio.to_thread——babysit 进程的线程只在
    poll/join 上休眠，不占 GIL。
    """
    ctx = multiprocessing.get_context("fork" if os.name == "posix" else "spawn")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_extract_worker, args=(page, send_conn), daemon=True)
    proc.start()
    send_conn.close()  # 关闭父进程持有的写端，EOF 语义才正确
    try:
        result = recv_conn.recv() if recv_conn.poll(timeout) else None
    except (EOFError, OSError):
        result = None
    finally:
        recv_conn.close()
    if proc.is_alive():
        proc.terminate()
        proc.join(1)
        if proc.is_alive():
            proc.kill()
    proc.join(1)
    return result


async def _fetch_local(url: str) -> tuple[str | None, str]:
    """直接抓取网页。返回 (成功文本, 失败说明)；文本为 None 表示这条路走不通。"""
    try:
        # wait_for 提供总时长上界：httpx 的 read 超时只管字节间隔，
        # 滴漏式慢服务器可以把单次抓取拖到分钟级
        resp = await asyncio.wait_for(
            _http.get(url, timeout=config.SEARCH_TIMEOUT,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; tgrok-bot/1.0)"}),
            config.SEARCH_TIMEOUT + 3,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("直接读取网页失败 %s: %s", url, type(e).__name__)
        return None, t("fetch_error", error=type(e).__name__)
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype and not (ctype.startswith("text/") or ctype in ("application/json", "application/xhtml+xml")):
        return None, t("fetch_unsupported", ctype=ctype)
    raw = resp.text[:200_000]  # 粗截超大页面，再做正文提取
    is_html = "html" in ctype or "<html" in raw[:2000].lower()
    if is_html:
        extracted = await asyncio.to_thread(_extract_in_process, raw, EXTRACT_TIMEOUT)
        if extracted is None:
            logger.warning("正文提取超时（%.0fs 强杀），回退简易提取: %s", EXTRACT_TIMEOUT, url)
            title, text = _html_to_text(raw)
            text = text.strip()
        else:
            title, text = extracted
    else:
        title, text = "", raw.strip()
    if not text or (is_html and len(text) < 200):
        # 正文过少：多半是 JS 渲染的空壳页，交给 Jina Reader 兜底
        return None, t("fetch_empty")
    header = f"{title}\n{resp.url}" if title else str(resp.url)
    images = _page_images(resp.text[:400_000], str(resp.url)) if is_html else []
    block = ("\n\n" + t("page_images") + "\n" + "\n".join(f"- {u}" for u in images)) if images else ""
    return f"{header}\n\n{text}"[:config.FETCH_CHAR_LIMIT - len(block)] + block, ""


_META_IMAGE_RE = re.compile(
    r"<meta\b[^>]*(?:property|name)\s*=\s*[\"'](?:og:image|og:image:url|twitter:image)[\"'][^>]*>", re.I)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"\b(content|src|data-src|data-original)\s*=\s*[\"']([^\"']+)[\"']", re.I)
_SKIP_IMAGE_RE = re.compile(r"(?:^data:|\.svg(?:$|\?)|sprite|icon|logo|avatar|blank|spacer|pixel|1x1)", re.I)
PAGE_IMAGES = 5


def _page_images(page: str, base: str) -> list[str]:
    """网页里能直接发的图片地址：先取 og:image / twitter:image（页面主图），再取正文里的 <img>，跳过图标和占位图。"""
    found: list[str] = []
    for tag in _META_IMAGE_RE.findall(page) + _IMG_RE.findall(page):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        src = attrs.get("content") or attrs.get("data-original") or attrs.get("data-src") or attrs.get("src") or ""
        src = urljoin(base, html.unescape(src.strip()))
        if src and not _SKIP_IMAGE_RE.search(src) and _is_public_http_url(src) and src not in found:
            found.append(src)
        if len(found) >= PAGE_IMAGES:
            break
    return found


IMAGE_MAX_BYTES = 10 * 1024 * 1024  # Telegram 照片上限 10MB
IMAGE_REDIRECTS = 5


async def fetch_image(url: str) -> tuple[bytes, str]:
    """下载一张公网图片，返回（内容, Content-Type）。每次重定向都重新校验地址（不让跳转把请求带到内网），
    边下边数大小，超过上限就停；不是图片、太大或下载失败时抛 WebError。"""
    url = (url or "").strip()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; tgrok-bot/1.0)"}
    for _ in range(IMAGE_REDIRECTS + 1):
        if not _is_public_http_url(url):
            raise WebError(t("fetch_bad_url"))
        try:
            async with asyncio.timeout(config.SEARCH_TIMEOUT + 10):
                async with _http.stream("GET", url, headers=headers, timeout=config.SEARCH_TIMEOUT,
                                        follow_redirects=False) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        url = urljoin(url, resp.headers["location"])
                        continue
                    resp.raise_for_status()
                    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if ctype and not ctype.startswith("image/"):
                        raise WebError(t("image_not_image", ctype=ctype))
                    data = bytearray()
                    async for chunk in resp.aiter_bytes():
                        data += chunk
                        if len(data) > IMAGE_MAX_BYTES:
                            raise WebError(t("image_too_large"))
                    return bytes(data), ctype
        except WebError:
            raise
        except Exception as e:
            logger.warning("下载图片失败 %s: %s", url, type(e).__name__)
            raise WebError(t("image_fetch_error", error=type(e).__name__)) from None
    raise WebError(t("image_fetch_error", error="too many redirects"))


async def _images_duckduckgo(query: str, n: int) -> list[str]:
    from ddgs import DDGS

    def _run() -> list[dict]:
        with DDGS(timeout=config.SEARCH_TIMEOUT) as ddgs:
            return list(ddgs.images(query, max_results=n, safesearch="moderate"))

    return [r.get("image") or "" for r in await asyncio.to_thread(_run)]


async def _images_serper(query: str, n: int) -> list[str]:
    resp = await _http.post("https://google.serper.dev/images",
                            headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
                            json={"q": query, "num": n}, timeout=config.SEARCH_TIMEOUT)
    resp.raise_for_status()
    return [r.get("imageUrl") or "" for r in resp.json().get("images") or []]


async def search_images(query: str, n: int = 8) -> list[str]:
    """按关键词找图片直链：Serper（配了 key 时）和 DuckDuckGo 并发，结果交替合并、去重。"""
    sources = [_images_duckduckgo(query, n)] + ([_images_serper(query, n)] if config.SERPER_API_KEY else [])
    results = await asyncio.gather(*sources, return_exceptions=True)
    lists = []
    for r in results:
        if isinstance(r, BaseException):
            logger.warning("图片搜索失败（%s）：%s", query, type(r).__name__)
        else:
            lists.append([u for u in r if _is_public_http_url(u)])
    merged: list[str] = []
    for i in range(max(map(len, lists), default=0)):  # 各来源轮流取一条
        for row in lists:
            if i < len(row) and row[i] not in merged:
                merged.append(row[i])
    return merged[:n]


_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


async def _fetch_jina(url: str) -> str | None:
    """通过 Jina Reader（r.jina.ai）抓取：其服务端渲染 JS 并输出 LLM 友好的 markdown。"""
    headers = {"X-Return-Format": "markdown", "X-Retain-Images": "none"}
    if config.JINA_API_KEY:
        headers["Authorization"] = f"Bearer {config.JINA_API_KEY}"
    try:
        resp = await asyncio.wait_for(
            _http.get(f"https://r.jina.ai/{url}", headers=headers, timeout=config.SEARCH_TIMEOUT * 2),
            config.SEARCH_TIMEOUT * 2 + 6,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Jina Reader 读取失败 %s: %s", url, type(e).__name__)
        return None
    # 内联链接压成纯文字，URL 不占正文字数配额（页面来源 URL 已在开头单独给出）
    text = _MD_LINK_RE.sub(r"\1", resp.text).strip()
    return text[:config.FETCH_CHAR_LIMIT] if text else None


async def run_fetch_url(url: str) -> str:
    """抓取网页正文：本地直取为主，Jina Reader 兜底；读不到时抛 WebError。"""
    url = (url or "").strip()
    if not _is_public_http_url(url):
        raise WebError(t("fetch_bad_url"))
    logger.info("读取网页: %s", url)
    t0 = time.monotonic()
    text, err = await _fetch_local(url)
    if text is not None:
        logger.info("读取网页完成（直取 %.1fs，%d 字）: %s", time.monotonic() - t0, len(text), url)
        return text
    if config.JINA_FALLBACK:
        logger.info("直取失败，改走 Jina Reader: %s", url)
        jina_text = await _fetch_jina(url)
        if jina_text:
            logger.info(
                "读取网页完成（Jina 兜底，共 %.1fs，%d 字）: %s", time.monotonic() - t0, len(jina_text), url
            )
            return jina_text
    raise WebError(err)
