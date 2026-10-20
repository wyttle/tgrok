"""一条回复在 Telegram 上的显示（下称「气泡」）：占位、流式编辑、长回复分段、工具进度和收尾。

气泡更新（占位、中间编辑、进度）都放在后台任务里单飞执行，同一时刻最多一个在途：
流消费不等 Telegram 往返、不被限流退避卡住，网络抖动也不会打断生成。
定稿、取消、报错前先 settle() 等在途更新落地，旧编辑不会覆盖新内容。
"""

import asyncio
import logging
import time
from collections.abc import Callable

from telegram import InlineKeyboardMarkup, Message
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from . import config, render
from .i18n import t
from .tools import ProgressLine

logger = logging.getLogger(__name__)

# 还没出正文时多久刷新一次进度（计时）。Telegram 建议同一聊天每秒最多一条，群里每分钟最多约 20 条，
# 编辑也算在内，所以群里放慢
TICK_PRIVATE, TICK_GROUP = 2.0, 4.0
TYPING_SECONDS = 4.5  # 「正在输入」状态在客户端显示约 5 秒，到期前续一次
SHOW_ELAPSED_AFTER = 3  # 等满这么多秒才显示计时，秒回的请求不闪一下数字


def elapsed_text(seconds: float) -> str:
    s = int(seconds)
    return t("elapsed_s", s=s) if s < 60 else t("elapsed_m", m=s // 60, s=s % 60)


async def _quiet(coro) -> None:
    """后台气泡更新：Telegram 网络抖动只影响这一次显示，不能打断生成。"""
    try:
        await coro
    except TelegramError as e:
        logger.warning("更新回复气泡失败：%s: %s", type(e).__name__, e)


class Bubble:
    def __init__(self, msg: Message, markup: InlineKeyboardMarkup, abort: Callable[[], None]):
        self.msg = msg
        self.markup = markup  # 生成过程中挂在气泡上的取消按钮
        self._abort = abort  # 占位消息都发不出去时放弃整次生成
        self.sent: Message | None = None  # 当前正在编辑的那条消息
        self.finalized = ""  # 已定稿的分段消息承载的文本
        self.answer_ids: list[int] = []  # 已定稿的分段消息 id
        self.segment = ""  # 当前消息正在累积的正文
        self.progress: list[ProgressLine] = []  # 工具行 + 两空格缩进的结果行
        self.stage: str | None = None  # 底部状态行（思考阶段），原地替换而非追加
        self.placeholder_failed = False
        group = getattr(getattr(msg, "chat", None), "type", "private") != "private"
        self._tick_seconds = TICK_GROUP if group else TICK_PRIVATE
        self._edit_interval = config.STREAM_EDIT_INTERVAL_GROUP if group else config.STREAM_EDIT_INTERVAL_PRIVATE
        self._t0 = time.monotonic()
        self._last_edit = 0.0  # 上一次正文编辑
        self._last_render = 0.0  # 上一次任何显示更新（占位、进度、正文），进度刷新据此限速
        self._task: asyncio.Task | None = None
        self._ticker: asyncio.Task | None = None
        self._typing: asyncio.Task | None = None
        self._live = True

    @property
    def has_text(self) -> bool:
        return bool(self.segment.strip())

    def output_len(self) -> int:
        """已经显示出去的正文量，用来判断一次失败的模型调用有没有吐出过内容。"""
        return len(self.finalized) + len(self.segment.strip())

    def open(self, stage: str) -> None:
        """发占位消息（和首轮模型请求并发），开始计时刷新进度，并持续显示「正在输入」。"""
        self.stage = stage
        self._t0 = time.monotonic()
        self._start(self._send_placeholder(f"{stage}…"))
        self._last_render = time.monotonic()
        self._ticker = asyncio.create_task(self._tick())
        self._typing = asyncio.create_task(self._keep_typing())

    async def settle(self) -> None:
        while self._task is not None:
            pending = self._task
            await asyncio.shield(pending)  # 调用方被取消时不连带取消在途的编辑
            if self._task is pending:
                self._task = None

    def _start(self, coro) -> None:
        """调用方须保证此前的更新已 settle()。"""
        self._task = asyncio.create_task(_quiet(coro))

    def _busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _send_placeholder(self, text: str) -> None:
        for attempt in range(2):
            try:
                self.sent = await self.msg.reply_text(text, reply_markup=self.markup)
                return
            except RetryAfter as e:
                # 群里被限流：等一下再发，不能因此放弃整条回复（模型这时已经在生成了）
                if attempt:
                    break
                logger.warning("发送占位消息被限流，%.0f 秒后重试", float(e.retry_after))
                await asyncio.sleep(float(e.retry_after) + 0.5)
            except TelegramError:
                break
        logger.exception("发送占位消息失败，放弃本次生成")
        self.placeholder_failed = True
        if self._live:
            self._abort()

    async def push(self, text: str, final: bool) -> None:
        """渲染一版正文：定稿不带光标和按钮，过程中带光标和取消按钮。
        都优先 MarkdownV2，解析失败回退纯文本；限流时中间编辑直接跳过，定稿等待后重试一次；
        定稿编辑网络超时也重试一次，不然气泡会停在带光标的中间版本上。"""
        if final:
            md, plain, kb = render.to_telegram_markdown(text), text, None
        else:
            md, plain, kb = render.streaming_markdown(text), text + config.STREAM_CURSOR, self.markup

        async def deliver(body: str, mode: str | None) -> None:
            if self.sent is None:
                self.sent = await self.msg.reply_text(body, parse_mode=mode, reply_markup=kb)
            else:
                await self.sent.edit_text(body, parse_mode=mode, reply_markup=kb)

        for attempt in range(2):
            editing = self.sent is not None
            try:
                try:
                    await deliver(md, ParseMode.MARKDOWN_V2)
                except BadRequest as e:
                    if "not modified" in str(e).lower():
                        return
                    await deliver(plain, None)
                return
            except RetryAfter as e:
                await asyncio.sleep(float(e.retry_after) + 0.5)
                if not final:
                    return
            except BadRequest:
                return  # 例如 message is not modified
            except NetworkError:
                # 超时的编辑可能其实已经生效，重试时会得到 not modified；新发的消息不重试，免得发出两条
                if not final or attempt or not editing:
                    raise
                await asyncio.sleep(1)

    async def on_text(self, delta: str) -> None:
        """模型的正文增量。"""
        self.segment += delta
        # while 而非 if：网关可能把很长的正文压在一个 delta 里发来，必须能一次切成多条消息，
        # 否则超过 4096 的编辑会被 Telegram 拒绝、尾部丢失
        limit = config.STREAM_SEGMENT_LIMIT
        while len(self.segment) >= limit:
            # 优先在换行处断开，其次空格，实在没有就硬切
            cut = self.segment.rfind("\n", limit // 2, limit)
            if cut == -1:
                cut = self.segment.rfind(" ", limit // 2, limit)
            if cut == -1:
                cut = limit
            part, self.segment = render.shape(self.segment[:cut]), self.segment[cut:].lstrip("\n")
            await self.settle()
            await self.push(part, final=True)
            self.finalized += part + "\n"
            if self.sent is not None:
                self.answer_ids.append(self.sent.message_id)
            self.sent, self._last_edit = None, 0.0
        now = time.monotonic()
        # 上一次更新还在途（慢网络或限流退避中）就跳过本次，等它落地后的下一个 delta 再刷新
        if self.has_text and now - self._last_edit >= self._edit_interval and not self._busy():
            await self.settle()
            self._start(self.push(render.shape(self.segment), final=False))
            self._last_edit = self._last_render = now

    def take_draft(self) -> str:
        """模型这一轮边写正文边请求工具：这段正文只是过程草稿，工具结果回来后模型会重写完整回答，
        留着会造成草稿和终稿拼接重复。清空并交给调用方保管（终轮空手时兜底回用）。
        已发出的中间编辑会被后续内容原地覆盖；超长草稿已定稿的分段无法撤回，属可接受损耗。"""
        draft, self.segment = self.segment, ""
        return draft

    def _since(self, start: float, now: float) -> str:
        seconds = now - start
        return " " + elapsed_text(seconds) if seconds >= SHOW_ELAPSED_AFTER else ""

    def _progress_body(self) -> str:
        """进度文本：纯文本，层级只靠缩进；底部一行是当前思考阶段，带从提问起的总计时。
        还在执行的工具行带这个工具的计时，长时间的调研也看得出没卡住。
        已有部分正文时正文在上、进度在下。在调度时取快照，后台执行时状态可能已前进。"""
        now = time.monotonic()
        lines = []
        for line in self.progress[-5:]:
            if line.result is None and not self.stage:  # 没有底部状态行时就是工具还在执行
                lines.append(line.text + "…" + self._since(line.started, now))
            else:
                lines.append(line.text)
            if line.result:
                lines.append(f"  {line.result}")
        if self.stage:
            lines.append(self.stage + "…" + self._since(self._t0, now))
        body = "\n".join(lines)
        if self.has_text:
            body = self.segment.rstrip() + "\n\n" + body
        # Telegram 上限 4096：截头保尾，进度在底部必须可见
        return body[-4000:]

    async def _render_progress(self, body: str) -> None:
        self._last_render = time.monotonic()
        try:
            if self.sent is None:
                self.sent = await self.msg.reply_text(body, reply_markup=self.markup)
            else:
                await self.sent.edit_text(body, reply_markup=self.markup)
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
        except TelegramError:
            pass
        self._last_edit = 0.0  # 让下一次正文编辑立即生效

    async def show_progress(self) -> None:
        """在后台渲染进度，从不阻塞随后的工具执行或下一轮请求。
        上一次更新还在途（慢网络、被限流后在等）或离上次显示太近就跳过：工具开始、出结果、换阶段常常挨在一起，
        群里连发会被限流；还没出正文时 ticker 每个周期都会刷新，跳过的状态最迟下个周期显示出来。"""
        if self._busy() or (not self.has_text and time.monotonic() - self._last_render < self._edit_interval):
            return
        await self.settle()
        self._start(self._render_progress(self._progress_body()))

    async def set_stage(self, stage: str | None) -> None:
        self.stage = stage
        await self.show_progress()

    async def _tick(self) -> None:
        # 还没出正文时定期刷新进度：计时在走，证明 bot 还活着
        while True:
            await asyncio.sleep(self._tick_seconds)
            if self.has_text or self._busy() or time.monotonic() - self._last_render < self._edit_interval:
                continue  # 正文已接管气泡、上一次更新还在途，或刚因工具/阶段变化刷新过
            await self.settle()
            self._start(self._render_progress(self._progress_body()))

    async def _keep_typing(self) -> None:
        while True:
            try:
                await self.msg.reply_chat_action(ChatAction.TYPING)
            except TelegramError:
                pass
            await asyncio.sleep(TYPING_SECONDS)

    def stop_ticker(self) -> None:
        for task in (self._ticker, self._typing):
            if task is not None:
                task.cancel()

    async def stop(self) -> None:
        """生成结束：停掉 ticker 并等在途更新落地，此后由调用方独占气泡。"""
        self._live = False
        self.stop_ticker()
        await self.settle()

    async def show_cancelled(self) -> None:
        """保留已有正文并标注已取消；还没输出正文就把占位改成「已取消」。"""
        try:
            if self.has_text:
                await self.push(render.shape(self.segment).rstrip() + "\n\n" + t("cancelled_suffix"), final=True)
            elif self.sent is not None and not self.finalized:
                await self.sent.edit_text(t("cancelled"))
        except TelegramError:
            pass

    async def show_error(self, text: str) -> None:
        """已有部分正文时保留定稿、错误另发一条；否则把占位改成错误提示。"""
        try:
            if self.has_text:
                await self.push(render.shape(self.segment), final=True)
                await self.msg.reply_text(text)
            elif self.sent is not None and not self.finalized:
                await self.sent.edit_text(text)
            else:
                await self.msg.reply_text(text)
        except TelegramError:
            pass

    async def finish(self, note: str, draft: str) -> tuple[list[int], str]:
        """定稿最后一段。note 是只用于显示的附注（备用模型提示），不计入返回的正文。
        返回（承载回复的全部消息 id, 完整正文）；没有任何正文时提示空回复并返回 ([], "")。"""
        if not self.has_text and draft:
            self.segment = draft  # 终轮没有产出正文：回用最后一轮被丢弃的草稿，别让用户空手而归
        if self.has_text:
            self.segment = render.shape(self.segment)
            await self.push(self.segment + note, final=True)
            self.finalized += self.segment
            if self.sent is not None:
                self.answer_ids.append(self.sent.message_id)
        elif self.finalized and note:
            try:
                await self.msg.reply_text(note.strip())  # 正文恰好在分段边界结束：提示单独发一条
            except TelegramError:
                pass
        elif not self.finalized:
            try:
                if self.sent is not None:
                    await self.sent.edit_text(t("empty_reply"))
            except TelegramError:
                pass
            return [], ""
        return self.answer_ids, self.finalized.strip()
