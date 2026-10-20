"""一次回复的生成：模型轮次循环（重试、备用模型、工具调用）和取消按钮。

显示交给 bubble.Bubble，工具交给 tools；这里只管「问模型 → 执行工具 → 再问」的流程。
"""

import asyncio
import logging
import time
from itertools import count

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from . import config, llm, prompt, tools
from .bubble import Bubble
from .i18n import phrases, t
from .tg_auth import is_admin

logger = logging.getLogger(__name__)

_gen_count = count(1)
# 正在生成的回复：gen_id → (任务, 提问者 id)，供取消按钮定位
active_generations: dict[int, tuple[asyncio.Task, int]] = {}


def _stage_line(round_idx: int) -> str:
    """第 N 轮生成对应的阶段文案（首轮「思考中」，拿到工具结果后「根据结果继续思考」），超出取最后一档。"""
    stages = phrases("thinking_stages")
    return stages[min(round_idx, len(stages) - 1)]


def _cancel_markup(gen_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t("btn_cancel"), callback_data=f"c:{gen_id}")]])


def _without_vendor_extras(history: list[dict]) -> list[dict]:
    """去掉工具调用上的厂商扩展字段（思考签名、加密推理条目等）。
    这些字段只有产生它们的那个模型/协议认得，交给另一个后端可能直接 400。"""
    out = []
    for m in history:
        if m.get("tool_calls") and any("extra_content" in c for c in m["tool_calls"]):
            m = {**m, "tool_calls": [{k: v for k, v in c.items() if k != "extra_content"}
                                     for c in m["tool_calls"]]}
        out.append(m)
    return out


class _Models:
    """本条回复用的模型：主模型出错且还没输出时，剩下的轮次整条换成备用模型。"""

    def __init__(self):
        self.adapter = llm.adapter
        self.fallback = llm.fallback_adapter
        self.using_fallback = False

    def can_switch(self) -> bool:
        return self.fallback is not None and not self.using_fallback

    def switch(self, working: list[dict]) -> list[dict]:
        """换成备用模型，返回改造过的对话：去掉厂商扩展字段；两边是否走工具循环不同时，
        系统提示词里的工具说明也跟着换。"""
        before, self.adapter, self.using_fallback = self.adapter, self.fallback, True
        working = _without_vendor_extras(working)
        if (working and working[0]["role"] == "system"
                and before.supports_tool_loop != self.adapter.supports_tool_loop):
            working[0] = {"role": "system", "content": prompt.system_prompt(self.adapter.supports_tool_loop)}
        return working


def _tool_schemas(models: _Models, round_idx: int) -> list[dict] | None:
    # 最后一轮不挂工具，强制模型输出正文，防止无限连环调用工具
    if models.adapter.supports_tool_loop and round_idx < config.SEARCH_MAX_ROUNDS:
        return tools.schemas()
    return None


async def _call_model(models: _Models, working: list[dict], round_idx: int,
                      bubble: Bubble) -> tuple[llm.RoundResult, list[dict], list[dict] | None]:
    """一轮模型调用。零输出断流重试一次；主模型出错且这轮还没输出时换备用模型（备用模型只试一次）；
    已有正文后断流按完成处理；429 不重试。返回（结果, 可能改造过的对话, 这轮挂的工具）。"""
    schemas = _tool_schemas(models, round_idx)
    for attempt in range(2):
        out_before = bubble.output_len()
        t0 = time.monotonic()
        try:
            result = await models.adapter.run_round(working, schemas, bubble.on_text)
            break
        except Exception as e:
            elapsed = time.monotonic() - t0
            if bubble.output_len() != out_before:
                # 正文已经到手、流在收尾阶段被上游掐断：按完成处理而非失败
                logger.warning("LLM 流中断但正文已到手（round=%d, %.1fs, %s），按完成处理",
                               round_idx, elapsed, type(e).__name__)
                result = llm.RoundResult(content=bubble.segment)
                break
            if models.can_switch():
                logger.warning("主模型 %s 调用失败（round=%d, %.1fs, %s: %.200s），切换备用模型 %s",
                               getattr(models.adapter, "model", ""), round_idx, elapsed, type(e).__name__, e,
                               getattr(models.fallback, "model", ""))
                working = models.switch(working)
                schemas = _tool_schemas(models, round_idx)
                if not bubble.has_text:
                    await bubble.set_stage(t("fallback_stage"))
                continue
            if attempt or llm.is_quota_error(e):
                raise
            logger.warning("LLM 流中断且本轮无输出（round=%d, %.1fs, %s），重试一次",
                           round_idx, elapsed, type(e).__name__)
            await asyncio.sleep(1.5)
    logger.info("LLM round=%d 完成 %.1fs：正文 %d 字，工具请求 %d 项，引用 %d 条",
                round_idx, time.monotonic() - t0, len(result.content), len(result.calls), len(result.citations))
    return result, working, schemas


async def _generate(history: list[dict], bubble: Bubble, chat_id: int, models: _Models) -> str:
    """跑模型轮次直到出终稿（正文留在 bubble 里），返回最后一版被丢弃的草稿。
    工具消息只追加到副本，调用方的 history 保持干净。"""
    working = list(history)
    citations: list[dict] = []
    draft = ""
    for round_idx in range(config.SEARCH_MAX_ROUNDS + 1):
        if round_idx and not bubble.has_text:
            await bubble.set_stage(_stage_line(round_idx))
        result, working, schemas = await _call_model(models, working, round_idx, bubble)
        if result.citations:
            citations = result.citations
        if not result.calls or not schemas:
            break
        if bubble.has_text:
            draft = bubble.take_draft()
        assistant_msg = llm.assistant_tool_call_msg(result.calls, result.content)
        plan = tools.plan_round(assistant_msg["tool_calls"], chat_id)
        # 执行期间撤下底部状态行，完成后挂上结果行，下一轮的思考阶段行再顶上
        bubble.progress.extend(plan.lines)
        bubble.stage = None
        await bubble.show_progress()
        working.append(assistant_msg)
        working.extend(await plan.execute())
    if citations and bubble.has_text:
        # Gemini 原生 grounding 的引用附在正文末尾
        links = "\n".join(f"[{c['title']}]({c['uri']})" for c in citations[:5])
        bubble.segment += "\n\n" + t("sources") + "\n" + links
    return draft


async def stream_reply(msg: Message, history: list[dict]) -> tuple[list[int], str]:
    """生成回复并在 Telegram 上流式显示。

    返回（承载回复的全部消息 id, 完整回复文本）——长回复分段时每一段都能被回复追问；
    失败、取消或空回复时已就地提示，返回 ([], "")。
    """
    gen_id = next(_gen_count)
    task = asyncio.current_task()
    requester = msg.from_user.id if msg.from_user else 0
    if task is not None:
        active_generations[gen_id] = (task, requester)
    bubble = Bubble(msg, _cancel_markup(gen_id), abort=task.cancel if task is not None else (lambda: None))
    bubble.open(_stage_line(0))
    models = _Models()
    try:
        try:
            draft = await _generate(history, bubble, msg.chat_id, models)
            await bubble.stop()
        except asyncio.CancelledError:
            # 提问者或管理员点了取消按钮（或占位消息发不出去）
            if task is not None and hasattr(task, "uncancel"):
                task.uncancel()
            await bubble.stop()
            if not bubble.placeholder_failed:
                logger.info("生成已被用户取消 chat=%s user=%s", msg.chat_id, requester)
                await bubble.show_cancelled()
            return [], ""
        except Exception as e:
            logger.exception("调用 LLM 失败")
            await bubble.stop()
            await bubble.show_error(t("llm_quota") if llm.is_quota_error(e) else t("llm_failed"))
            return [], ""
        finally:
            bubble.stop_ticker()
        # 用了备用模型就在回复末尾注明；提示只用于显示，不进对话历史
        note = "\n\n" + t("fallback_note", model=getattr(models.adapter, "model", "")) if models.using_fallback else ""
        return await bubble.finish(note, draft)
    finally:
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
