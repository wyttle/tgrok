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


def _adapt(working: list[dict], before, after) -> list[dict]:
    """对话交给另一个模型前的改造：去掉厂商扩展字段；两边是否走工具循环不同时，系统提示词里的工具说明也跟着换。"""
    working = _without_vendor_extras(working)
    if working and working[0]["role"] == "system" and before.supports_tool_loop != after.supports_tool_loop:
        working[0] = {"role": "system", "content": prompt.system_prompt(after.supports_tool_loop)}
    return working


class _Models:
    """本条回复用的模型：主模型出错且还没输出时，剩下的轮次整条换成备用模型；
    首轮太慢被帮手模型抢答时，整条换成帮手模型。note 是回复末尾说明换了谁的附注（只显示，不进历史）。"""

    def __init__(self):
        self.adapter = llm.adapter
        self.fallback = llm.fallback_adapter
        self.using_fallback = False
        self.note = ""

    def can_switch(self) -> bool:
        return self.fallback is not None and not self.using_fallback

    def switch(self, working: list[dict]) -> list[dict]:
        """换成备用模型，返回改造过的对话。"""
        before, self.adapter, self.using_fallback = self.adapter, self.fallback, True
        self.note = "\n\n" + t("fallback_note", model=getattr(self.adapter, "model", ""))
        return _adapt(working, before, self.adapter)

    def hedge(self):
        """可以拿来抢答的帮手模型；没配、关掉了、已经换过模型，或者帮手就是主模型本身时为 None。"""
        helper = llm.helper_adapter
        if helper is None or config.HEDGE_AFTER <= 0 or self.using_fallback:
            return None
        if helper.name == self.adapter.name and helper.model == getattr(self.adapter, "model", None):
            return None
        return helper

    def adopt(self, helper) -> None:
        self.adapter, self.using_fallback = helper, True
        self.note = "\n\n" + t("hedge_note", model=getattr(helper, "model", ""))


class _Lane:
    """一路模型输出：live 时直接交给气泡；抢答期间先攒着，胜出后再一次放出来。"""

    def __init__(self, bubble: Bubble, live: bool):
        self.bubble, self.live, self.buf = bubble, live, []
        self.spoke = asyncio.Event()

    async def on_text(self, delta: str) -> None:
        if self.live:
            await self.bubble.on_text(delta)
        else:
            self.buf.append(delta)
        if delta.strip():
            self.spoke.set()

    async def go_live(self) -> None:
        self.live = True
        text, self.buf = "".join(self.buf), []
        if text:
            await self.bubble.on_text(text)


def _useful(task: asyncio.Task) -> bool:
    """这一路已经跑完并且有东西（正文或工具调用）。"""
    if not task.done() or task.cancelled() or task.exception() is not None:
        return False
    result = task.result()
    return bool(result.calls or result.content.strip())


def _drop(*tasks: asyncio.Task) -> None:
    for task in tasks:
        task.cancel()
        # 被取消的一路可能稍后才抛出别的异常：取走，免得事件循环报「异常没人取」
        task.add_done_callback(lambda t: t.cancelled() or t.exception())


async def _race(models: _Models, working: list[dict], schemas: list[dict] | None,
                bubble: Bubble) -> tuple[llm.RoundResult, list[dict], list[dict] | None]:
    """首轮：主模型 HEDGE_AFTER 秒内既没出字也没跑完（没开始调工具），就同时让帮手模型（Gemini 原生，
    自带 Google 搜索）作答，谁先出字用谁，另一个取消。一路出错时另一路继续，两路都失败才把主模型的错误抛出。"""
    helper = models.hedge()
    if helper is None:
        return await models.adapter.run_round(working, schemas, bubble.on_text), working, schemas
    primary = _Lane(bubble, live=True)
    p_task = asyncio.create_task(models.adapter.run_round(working, schemas, primary.on_text))
    p_spoke = asyncio.create_task(primary.spoke.wait())
    waiters, h_task = [p_spoke], None
    try:
        done, _ = await asyncio.wait({p_task, p_spoke}, timeout=config.HEDGE_AFTER,
                                     return_when=asyncio.FIRST_COMPLETED)
        if done:
            return await p_task, working, schemas
        primary.live = False  # 从这里起两路都先攒着
        h_working = _adapt(working, models.adapter, helper)
        hedge = _Lane(bubble, live=False)
        h_task = asyncio.create_task(helper.run_round(h_working, None, hedge.on_text))
        h_spoke = asyncio.create_task(hedge.spoke.wait())
        waiters.append(h_spoke)
        logger.info("主模型 %.0f 秒没有输出，同时让 %s 抢答", config.HEDGE_AFTER, helper.model)
        await bubble.set_stage(t("hedge_stage", model=helper.model))
        pending = {p_task, h_task, p_spoke, h_spoke}
        while True:
            _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if primary.spoke.is_set() or _useful(p_task):
                _drop(h_task)
                await primary.go_live()
                return await p_task, working, schemas
            if hedge.spoke.is_set() or _useful(h_task):
                _drop(p_task)
                logger.info("%s 抢答胜出", helper.model)
                models.adopt(helper)
                await hedge.go_live()
                return await h_task, h_working, None
            if p_task.done() and h_task.done():
                if h_task.exception() is not None:
                    logger.warning("抢答的 %s 也失败了：%s", helper.model, h_task.exception())
                return await p_task, working, schemas  # 主模型的错误照常交给外层（重试、备用模型）
    except BaseException:  # 被取消（用户点了取消）或主模型出错：还在跑的那路一并停掉
        _drop(p_task, *([h_task] if h_task is not None else []))
        raise
    finally:
        for waiter in waiters:
            waiter.cancel()


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
            if round_idx == 0 and attempt == 0:
                result, working, schemas = await _race(models, working, schemas, bubble)
            else:
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
        # 换了备用模型或被帮手模型抢答，就在回复末尾注明；提示只用于显示，不进对话历史
        return await bubble.finish(models.note, draft)
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
