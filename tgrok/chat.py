"""会话层：进度显示、工具执行、流式回复主循环与取消按钮。"""

import asyncio
import logging
import re
import time
from itertools import count

import telegramify_markdown
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from . import config, llm, web
from .config import STREAM_CURSOR, STREAM_EDIT_INTERVAL, STREAM_SEGMENT_LIMIT
from .i18n import STRINGS, t
from .config import BOT_LANG
from .tg_auth import is_admin

logger = logging.getLogger(__name__)

# 匹配标题行：行首可选的符号前缀 + 1~6 个 #。Grok 有时输出带符号前缀的标题，
# 此时 # 不在行首，telegramify/CommonMark 不认作标题，会把 ## 原样
# 泄漏成 \#\#。这里去掉前缀符号、让 # 回到行首，使下游正常渲染成加粗。
_HEADING_RE = re.compile(r"^[ \t]*[^\w#\n]*[ \t]*(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)


def _normalize_headings(text: str) -> str:
    def repl(m: re.Match) -> str:
        return f"{m.group(1)} {m.group(2)}"
    return _HEADING_RE.sub(repl, text)


def to_telegram_markdown(text: str) -> str:
    """把模型返回的标准 Markdown 转成 Telegram MarkdownV2。

    先归一化标题行（去掉 Grok 添加的符号前缀，让 # 回到行首），再交给
    telegramify_markdown 转换。
    """
    return telegramify_markdown.markdownify(_normalize_headings(text))

_RESULT_ROW_RE = re.compile(r"^\[\d+\]", re.M)


def _stage_line(round_idx: int) -> str:
    """第 N 轮生成对应的阶段文案（思考中 → 深入思考中 → …），超出取最后一档。"""
    stages = STRINGS[BOT_LANG]["thinking_stages"]
    return stages[min(round_idx, len(stages) - 1)]


def _round_entries(calls_list: list[dict]) -> tuple[list[dict], list[dict]]:
    """把一轮工具调用变成进度条目。

    搜索每条一行（显示搜索词）；grounding 模式下同轮多个搜索合并为一行
    （执行时也会合并为一次调研）。同一轮的多个网页读取合并为一行，
    结果聚合为总字数。不暴露 URL/域名给群成员。
    返回 (条目列表, 逐调用到条目的映射)。
    """
    entries: list[dict] = []
    mapping: list[dict] = []
    open_entry: dict | None = None
    search_calls = [c for c in calls_list if c["function"]["name"] != "open_url"]
    merge_search = bool(config.GEMINI_SEARCH_MODEL) and len(search_calls) > 1
    merged_search_entry: dict | None = None
    for call in calls_list:
        args = llm._tool_args(call) or {}
        if call["function"]["name"] == "open_url":
            if open_entry is None:
                open_entry = {"kind": "open", "count": 0, "contents": [],
                              "text": "", "done": False, "result": None}
                entries.append(open_entry)
            open_entry["count"] += 1
            mapping.append(open_entry)
        elif merge_search:
            if merged_search_entry is None:
                queries = "；".join(
                    str((llm._tool_args(c) or {}).get("query", "")).strip() or "?" for c in search_calls
                )
                merged_search_entry = {"kind": "search", "count": len(search_calls), "contents": [],
                                       "text": t("tool_search", q=queries[:48]),
                                       "done": False, "result": None}
                entries.append(merged_search_entry)
            mapping.append(merged_search_entry)
        else:
            query = str(args.get("query", "")).strip() or "?"
            entry = {"kind": "search", "count": 1, "contents": [],
                     "text": t("tool_search", q=query[:48]), "done": False, "result": None}
            entries.append(entry)
            mapping.append(entry)
    if open_entry is not None:
        open_entry["text"] = (
            t("tool_open") if open_entry["count"] == 1 else t("tool_open_n", n=open_entry["count"])
        )
    return entries, mapping


def _human_chars(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _attach_result(entry: dict, call: dict, content: str) -> None:
    """把一次工具执行的结果记到对应条目上；合并条目聚合后再定稿。"""
    if entry["kind"] == "open":
        entry["contents"].append(content)
        if len(entry["contents"]) < entry["count"]:
            return
        ok = [c for c in entry["contents"] if not c.startswith(("（", "("))]
        fails = entry["count"] - len(ok)
        if ok:
            result = t("res_chars", k=_human_chars(sum(len(c) for c in ok)))
            if fails:
                result += t("res_fail_suffix", n=fails)
        else:
            result = t("res_failed")
        entry["done"], entry["result"] = True, result
    elif entry.get("count", 1) > 1:
        # 合并执行的多个搜索：真实结果只在其中一条（其余是"已合并"占位）
        entry["contents"].append(content)
        if len(entry["contents"]) < entry["count"]:
            return
        real = next((c for c in entry["contents"] if not c.startswith(("（", "("))), "")
        entry["done"] = True
        entry["result"] = _result_summary("web_search", real) if real else t("res_failed")
    else:
        entry["done"] = True
        entry["result"] = _result_summary(call["function"]["name"], content)


def _result_summary(tool_name: str, content: str) -> str:
    """工具结果的一行摘要：搜索 → N 条结果，读网页 → 字数，错误文案 → 失败。"""
    if content.startswith("（") or content.startswith("("):
        return t("res_failed")
    if tool_name == "open_url":
        n = len(content)
        return t("res_chars", k=f"{n / 1000:.1f}k" if n >= 1000 else str(n))
    rows = len(_RESULT_ROW_RE.findall(content))
    if rows:
        return t("res_results", n=rows)
    # grounding 综述没有 [n] 行标记，显示字数
    n = len(content)
    return t("res_chars", k=f"{n / 1000:.1f}k" if n >= 1000 else str(n))


async def _execute_tool_calls(assistant_msg: dict) -> list[dict]:
    """并发执行一轮内的所有工具调用（相互独立），按原顺序返回 tool 消息。

    grounding 模式下同一轮的多个 web_search 合并为一次深度调研（每次 grounding
    内部本就是多跳检索），结果给第一条，其余标注已合并——防止主模型把调研代理
    当浏览器逐条调度。
    """
    calls_list = assistant_msg["tool_calls"]
    merged_result: str | None = None
    first_search_i: int | None = None
    if config.GEMINI_SEARCH_MODEL:
        queries = []
        for i, call in enumerate(calls_list):
            if call["function"]["name"] != "open_url":
                query = str((llm._tool_args(call) or {}).get("query", "")).strip()
                if query:
                    if first_search_i is None:
                        first_search_i = i
                    queries.append(query)
        if len(queries) > 1:
            merged_result = await web.run_web_search("；".join(dict.fromkeys(queries)))

    async def run_one(i: int, call: dict) -> dict:
        name = call["function"]["name"]
        args = llm._tool_args(call)
        if args is None:
            content = t("search_bad_args")
        elif name == "open_url":
            content = await web.run_fetch_url(str(args.get("url", "")))
        elif merged_result is not None:
            content = merged_result if i == first_search_i else t("search_merged")
        else:
            content = await web.run_web_search(str(args.get("query", "")))
        return {"role": "tool", "tool_call_id": call["id"], "name": name, "content": content}

    return list(await asyncio.gather(*(run_one(i, c) for i, c in enumerate(calls_list))))

_gen_count = count(1)
active_generations: dict[int, tuple[asyncio.Task, int]] = {}


def _cancel_markup(gen_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("btn_cancel"), callback_data=f"c:{gen_id}")]]
    )


async def stream_reply(msg: Message, history: list[dict]) -> tuple[Message | None, str]:
    """流式生成并逐步编辑 Telegram 消息，支持模型通过 web_search 工具联网搜索。

    先发送思考占位提示（推理模型思考期间无正文输出），首个正文数据块到达后原地替换；
    单条消息超过 STREAM_SEGMENT_LIMIT 时定稿当前消息、另起一条继续。
    模型请求搜索时在当前消息上显示搜索状态，执行后把结果回灌给模型继续生成
    （最多 config.SEARCH_MAX_ROUNDS 轮）。传入的 history 不会被修改，中间的 tool
    消息只存在于本次调用内部，不会进入对话缓存。
    返回（最后一条已发送消息或 None, 完整回复文本）；失败/空回复时已就地提示，返回 (None, "")。
    """
    gen_id = next(_gen_count)
    task = asyncio.current_task()
    requester = msg.from_user.id if msg.from_user else 0
    if task is not None:
        active_generations[gen_id] = (task, requester)
    markup = _cancel_markup(gen_id)
    progress: list[dict] = []  # 工具条目 {text, done, result}：工具行 + 两空格缩进的结果行
    stage: str | None = _stage_line(0)  # 底部状态行：思考阶段文本，原地替换而非追加
    try:
        sent: Message | None = await msg.reply_text(f"{stage}…", reply_markup=markup)
    except TelegramError:
        logger.exception("发送占位消息失败")
        active_generations.pop(gen_id, None)
        return None, ""
    finalized = ""  # 已定稿消息承载的文本
    segment = ""  # 当前消息正在累积的文本
    last_edit = 0.0

    async def push(text: str, final: bool) -> None:
        nonlocal sent
        try:
            if sent is None:
                # 首次发送：定稿走 MarkdownV2（不带按钮），中间过程用纯文本+光标+取消按钮
                if final:
                    try:
                        sent = await msg.reply_text(
                            to_telegram_markdown(text), parse_mode=ParseMode.MARKDOWN_V2
                        )
                    except BadRequest:
                        sent = await msg.reply_text(text)
                else:
                    sent = await msg.reply_text(text + STREAM_CURSOR, reply_markup=markup)
            elif final:
                try:
                    await sent.edit_text(
                        to_telegram_markdown(text), parse_mode=ParseMode.MARKDOWN_V2
                    )
                except BadRequest:
                    await sent.edit_text(text)
            else:
                await sent.edit_text(text + STREAM_CURSOR, reply_markup=markup)
        except RetryAfter as e:
            # Telegram 限流：等待后跳过本次中间编辑；定稿编辑重试一次
            await asyncio.sleep(float(e.retry_after) + 0.5)
            if final and sent is not None:
                try:
                    await sent.edit_text(
                        to_telegram_markdown(text), parse_mode=ParseMode.MARKDOWN_V2
                    )
                except BadRequest:
                    try:
                        await sent.edit_text(text)
                    except TelegramError:
                        pass
                except TelegramError:
                    pass
        except BadRequest:
            pass  # 例如 message is not modified

    async def on_text(delta: str) -> None:
        nonlocal segment, sent, last_edit, finalized
        segment += delta
        # while 而非 if：网关可能把很长的正文压在一个 delta 里发来，
        # 必须能一次切成多条消息，否则超过 4096 的编辑会被 Telegram 拒绝、尾部丢失
        while len(segment) >= STREAM_SEGMENT_LIMIT:
            # 优先在换行处断开，其次空格，实在没有就硬切
            cut = segment.rfind("\n", STREAM_SEGMENT_LIMIT // 2, STREAM_SEGMENT_LIMIT)
            if cut == -1:
                cut = segment.rfind(" ", STREAM_SEGMENT_LIMIT // 2, STREAM_SEGMENT_LIMIT)
            if cut == -1:
                cut = STREAM_SEGMENT_LIMIT
            part, segment = segment[:cut], segment[cut:].lstrip("\n")
            await push(part, final=True)
            finalized += part + "\n"
            sent, last_edit = None, 0.0
        now = time.monotonic()
        if segment.strip() and now - last_edit >= STREAM_EDIT_INTERVAL:
            await push(segment, final=False)
            last_edit = now

    async def render_progress(suffix: str = "…") -> None:
        """渲染进度：纯文本无符号，层级只靠缩进——工具行顶格、结果行缩进两格，
        底部一行是当前思考阶段（原地替换，省略号由 ticker 变化）。
        已有部分正文时正文在上、日志在下。
        """
        nonlocal sent, last_edit
        lines = []
        for e in progress[-5:]:
            lines.append(e["text"])
            if e["result"]:
                lines.append(f"  {e['result']}")
        if stage:
            lines.append(stage + suffix)
        body = "\n".join(lines)
        if segment.strip():
            body = segment.rstrip() + "\n\n" + body
        if len(body) > 4000:
            body = body[-4000:]  # Telegram 上限 4096：截头保尾，进度日志在底部必须可见
        try:
            if sent is None:
                sent = await msg.reply_text(body, reply_markup=markup)
            else:
                await sent.edit_text(body, reply_markup=markup)
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
        except TelegramError:
            pass
        last_edit = 0.0  # 让下一次正文编辑立即生效

    async def _ticker() -> None:
        # 长时间等待时变化底部省略号，证明 bot 还活着
        frames = ["…", "……", "………"]
        i = 0
        while True:
            await asyncio.sleep(5)
            if segment.strip():
                continue  # 正文已开始流式输出，气泡由 on_text 接管
            i += 1
            await render_progress(frames[i % len(frames)])

    ticker_task = asyncio.create_task(_ticker())

    working = list(history)  # 工具消息只追加到副本，调用方的 history 保持干净
    generation_completed = False
    try:
        if config.GEMINI_NATIVE_SEARCH:
            # 原生模式：google_search/url_context 由 Google 服务端执行，单轮生成，
            # 重试/掐流/空闲语义与 OpenAI 路径一致
            citations: list[dict] = []
            t0 = time.monotonic()
            for attempt in range(2):
                out_before = len(finalized) + len(segment.strip())
                t0 = time.monotonic()
                try:
                    stream = await llm.gemini_create_stream(working)
                    citations, _ = await llm._drain_gemini_stream(stream, on_text)
                    break
                except Exception as e:
                    elapsed = time.monotonic() - t0
                    if len(finalized) + len(segment.strip()) != out_before:
                        logger.warning(
                            "Gemini 流中断但正文已到手（%.1fs, %s），按完成处理",
                            elapsed, type(e).__name__,
                        )
                        break
                    if attempt or llm._is_quota_error(e):
                        raise
                    logger.warning(
                        "Gemini 流中断且无输出（%.1fs, %s），重试一次", elapsed, type(e).__name__
                    )
                    await asyncio.sleep(1.5)
            logger.info(
                "Gemini 原生轮完成 %.1fs：正文 %d 字，引用 %d 条",
                time.monotonic() - t0, len(finalized) + len(segment), len(citations),
            )
            if citations and segment.strip():
                links = "\n".join(f"[{c['title']}]({c['uri']})" for c in citations[:5])
                segment += "\n\n" + t("sources") + "\n" + links
        rounds = 0 if config.GEMINI_NATIVE_SEARCH else config.SEARCH_MAX_ROUNDS + 1
        for round_idx in range(rounds):
            # 最后一轮不带 tools，强制模型输出正文，防止无限连环搜索
            use_tools = config.SEARCH_ENABLED and round_idx < config.SEARCH_MAX_ROUNDS
            if round_idx and not segment.strip():
                # 工具执行完、新一轮生成开始：底部状态行原地替换为下一档思考阶段
                stage = _stage_line(round_idx)
                await render_progress()
            t0 = time.monotonic()
            for attempt in range(2):
                out_before = len(finalized) + len(segment.strip())
                t0 = time.monotonic()
                try:
                    stream = await llm.create_stream(working, use_tools=use_tools)
                    calls, content = await llm._drain_stream(stream, on_text)
                    break
                except Exception as e:
                    elapsed = time.monotonic() - t0
                    if len(finalized) + len(segment.strip()) != out_before:
                        # 正文已经到手、流在收尾阶段被上游掐断：按完成处理而非失败
                        logger.warning(
                            "LLM 流中断但正文已到手（round=%d, %.1fs, %s），按完成处理",
                            round_idx, elapsed, type(e).__name__,
                        )
                        calls, content = {}, segment
                        break
                    if attempt or llm._is_quota_error(e):
                        raise
                    logger.warning(
                        "LLM 流中断且本轮无输出（round=%d, %.1fs, %s），重试一次",
                        round_idx, elapsed, type(e).__name__,
                    )
                    await asyncio.sleep(1.5)
            logger.info(
                "LLM round=%d 完成 %.1fs：正文 %d 字，工具请求 %d 项",
                round_idx, time.monotonic() - t0, len(content), len(calls),
            )
            if not calls or not use_tools:
                break
            assistant_msg = llm._assistant_tool_call_msg(calls, content)
            # 追加本轮工具条目（同轮多个网页读取合并为一行）；执行期间底部状态行
            # 撤下，完成后挂上缩进的结果行，下一轮的思考阶段行再顶上
            stage = None
            entries, call_map = _round_entries(assistant_msg["tool_calls"])
            progress.extend(entries)
            await render_progress()
            working.append(assistant_msg)
            tool_results = await _execute_tool_calls(assistant_msg)
            working.extend(tool_results)
            for entry, call, result in zip(call_map, assistant_msg["tool_calls"], tool_results):
                _attach_result(entry, call, result["content"])
    except asyncio.CancelledError:
        # 提问者/管理员点了取消按钮：保留已有正文并标注，未输出则改为已取消
        if task is not None and hasattr(task, "uncancel"):
            task.uncancel()
        logger.info("生成已被用户取消 chat=%s user=%s", msg.chat_id, requester)
        try:
            if segment.strip():
                await push(segment.rstrip() + "\n\n" + t("cancelled_suffix"), final=True)
            elif sent is not None and not finalized:
                await sent.edit_text(t("cancelled"))
        except TelegramError:
            pass
        return None, ""
    except Exception as e:
        logger.exception("调用 LLM 失败")
        fail_text = t("llm_quota") if llm._is_quota_error(e) else t("llm_failed")
        try:
            if segment.strip():
                # 已有部分内容：保留定稿，错误另发一条
                await push(segment, final=True)
                await msg.reply_text(fail_text)
            elif sent is not None and not finalized:
                await sent.edit_text(fail_text)
            else:
                await msg.reply_text(fail_text)
        except TelegramError:
            pass
        return None, ""
    finally:
        ticker_task.cancel()
        if not generation_completed:
            active_generations.pop(gen_id, None)

    try:
        if segment.strip():
            await push(segment, final=True)
            finalized += segment
        elif not finalized:
            try:
                if sent is not None:
                    await sent.edit_text(t("empty_reply"))
            except TelegramError:
                pass
            return None, ""
        return sent, finalized.strip()
    finally:
        generation_completed = True
        active_generations.pop(gen_id, None)


async def _answer_callback(q, text: str | None = None) -> None:
    try:
        await q.answer(text)
    except BadRequest as e:
        error = str(e).lower()
        if "query is too old" not in error and "query id is invalid" not in error:
            raise


async def on_cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """取消按钮回调：找到对应生成任务并 cancel。仅提问者本人或管理员可取消。"""
    q = update.callback_query
    if q is None or not q.data:
        return
    try:
        gen_id = int(q.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await _answer_callback(q)
        return
    entry = active_generations.get(gen_id)
    if entry is None:
        await _answer_callback(q, t("cancel_gone"))
        return
    task, owner = entry
    user = q.from_user
    if user is None or (user.id != owner and not is_admin(user.id)):
        await _answer_callback(q, t("cancel_denied"))
        return
    task.cancel()
    await _answer_callback(q, t("cancel_done"))
