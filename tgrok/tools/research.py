"""research：把调研任务同时交给几个 omp 研究员（不同模型），各自的报告一起交给主模型判断。

每个研究员是一个 omp 无界面进程（omp -p）：只开放 web_search 和 read，加载 research_hook.ts
把 read 限制在公网网页，在空的临时目录里运行，超时或回复被取消时连同子进程一起结束。
"""

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import signal
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .. import config, logs, usage
from ..i18n import t
from . import Outcome, Tool, failed, show_detail

logger = logging.getLogger(__name__)

HOOK = Path(__file__).with_name("research_hook.ts")
REPORT_CHARS = 6000  # 单份报告回灌给主模型的字数上限
READY_TIMEOUT = 90  # omp 冷启动（加载模型目录、建会话）最多等多久
RECYCLE_AFTER = 20  # 一个常驻 omp 接过这么多个任务就换新进程，免得内存越用越多

SYSTEM_PROMPT = (
    "You are a fact-finding researcher. Research the task with web_search and read (read only public web pages). "
    "Verify key facts by reading the actual pages, not just search snippets: primary sources, official sites, "
    "reputable media and encyclopedias, and for internet slang or niche community jargon the explainer pages and "
    "community posts (Moegirl, Baidu Baike, Zhihu, Bilibili, gaming and fan sites). If results are thin or "
    "conflicting, search again with different wording or in another language. Report in the language of the task: "
    "concise factual bullet points with dates where relevant; say plainly what you could not verify and where "
    "sources disagree. Then list the URLs you actually read. No opinions, no filler."
)


@dataclass
class Report:
    model: str
    text: str = ""
    error: str = ""
    secs: float = 0.0
    usage: dict = field(default_factory=dict)  # omp 报的累计用量：input、output、cacheRead、cacheWrite 等

    @property
    def tokens_in(self) -> int:
        return sum(self.usage.get(k, 0) for k in ("input", "cacheRead", "cacheWrite"))


def _command(model: str) -> list[str]:
    """常驻的 omp：--mode rpc 从 stdin 一行一个命令接任务，stdout 一行一个事件。"""
    return [
        *shlex.split(config.OMP_BIN, posix=os.name != "nt"),
        "--mode", "rpc", "--no-ui", "--no-session", "--no-title", "--no-skills", "--no-rules", "--no-extensions",
        "--no-lsp", "--tools", "web_search,read", "--hook", str(HOOK), "--model", model,
        "--thinking", config.RESEARCH_THINKING, "--system-prompt", SYSTEM_PROMPT,
    ]


_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _error_text(raw: str) -> str:
    """omp 报的接口错误压成一行：网关返回的 HTML 错误页只取标题（如「403 网站防火墙」）。"""
    title = _TITLE_RE.search(raw)
    if title:
        raw = raw[: raw.find("<")] + title.group(1)
    return " ".join(_TAG_RE.sub(" ", raw).split())[:160]


def parse_events(stdout: str) -> tuple[str, dict, str]:
    """omp 的事件流 → (最后一条回答的正文, 累计 token 用量, 失败原因)。
    失败原因取最后一条回答的结束原因：aborted 记成 timed out；error 取接口报的错误；正常结束为空串。"""
    text, usage, failure = "", {}, ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message") or {}
        if event.get("type") != "message_end" or message.get("role") != "assistant":
            continue
        parts = [p.get("text", "") for p in message.get("content") or []
                 if isinstance(p, dict) and p.get("type") == "text"]
        if "".join(parts).strip():
            text = "".join(parts)
        for key, value in (message.get("usage") or {}).items():
            if isinstance(value, int):
                usage[key] = usage.get(key, 0) + value
        stop = message.get("stopReason")
        failure = ("timed out" if stop == "aborted"
                   else _error_text(str(message.get("errorMessage") or "provider error")) if stop == "error" else "")
    return text.strip(), usage, failure


def _kill(proc: asyncio.subprocess.Process) -> None:
    """结束 omp 及它起的子进程（POSIX 下整组结束）。"""
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


_STREAM_LIMIT = 32 * 1024 * 1024  # omp 一行一个事件，读网页的结果可能很长
_PROGRESS_TYPES = ("agent_start", "tool_execution_start", "tool_execution_end")

Step = Callable[[str, int], None]  # （在做什么, 第几步）


def _short_url(url: str) -> str:
    url = re.sub(r"^https?://(www\.)?", "", str(url or "")).rstrip("/")
    return url if len(url) <= 40 else url[:39] + "…"


def _doing(event: dict) -> str | None:
    """进度里一句话说研究员在做什么；和进度无关的事件返回 None。"""
    kind = event.get("type")
    if kind != "tool_execution_start":
        return t("research_doing_think") if kind in ("agent_start", "tool_execution_end") else None
    args = event.get("args")
    args = args if isinstance(args, dict) else {}
    if event.get("toolName") == "web_search":
        query = " ".join(str(args.get("query", "")).split())
        return t("research_doing_search", q=query if len(query) <= 30 else query[:29] + "…")
    if event.get("toolName") == "read":
        return t("research_doing_read", what=_short_url(args.get("path", "")))
    return str(event.get("toolName") or "?")


class _Omp:
    """一个常驻的 omp 进程。冷启动要十几秒（加载模型目录、建会话），所以先启动好放着：
    接到任务立刻开工，下一个任务开一个新会话接着用；超时、出错或被取消时整个进程丢掉。"""

    def __init__(self, model: str):
        self.model = model
        self.proc: asyncio.subprocess.Process | None = None
        self.workdir = tempfile.mkdtemp(prefix="tgrok-research-")
        self.stderr: deque[str] = deque(maxlen=20)
        self.uses = 0
        self._stderr_task: asyncio.Task | None = None
        self.ready = asyncio.create_task(self._start())

    async def _start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *_command(self.model), cwd=self.workdir, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=os.name == "posix",
            limit=_STREAM_LIMIT)
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        async with asyncio.timeout(READY_TIMEOUT):
            while not _is_frame(await self._line(), "ready"):
                pass

    async def _drain_stderr(self) -> None:
        async for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self.stderr.append(line)

    async def _line(self) -> str:
        raw = await self.proc.stdout.readline()
        if not raw:
            await self.proc.wait()
            if self._stderr_task is not None:
                await self._stderr_task
            raise RuntimeError((self.stderr[-1][:200] if self.stderr else "")
                               or f"omp exited (code {self.proc.returncode})")
        return raw.decode("utf-8", "replace")

    async def _send(self, command: dict) -> None:
        self.proc.stdin.write((json.dumps(command, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    @property
    def alive(self) -> bool:
        return self.ready.done() and not self.ready.cancelled() and self.ready.exception() is None \
            and self.proc.returncode is None

    async def run(self, task: str, on_step: Step | None) -> tuple[str, dict, str, int]:
        """交一个任务，读事件直到它结束。返回（正文, 用量, 失败原因, 步数）。"""
        self.uses += 1
        if self.uses > 1:
            await self._send({"id": "fresh", "type": "new_session"})  # 上一个任务的上下文不带过来
        rid = f"task-{self.uses}"
        await self._send({"id": rid, "type": "prompt", "message": task})
        lines: list[str] = []
        steps, error = 0, ""
        while True:
            line = await self._line()
            head = line[:120]
            if _is_frame(line, "response"):
                frame = json.loads(line)
                if frame.get("success") is False:
                    raise RuntimeError(str(frame.get("error") or "omp rejected the task")[:200])
                continue
            if _is_frame(line, "prompt_result"):
                frame = json.loads(line)
                if frame.get("id") != rid:
                    continue
                if frame.get("status") == "error":
                    error = _error_text(str((frame.get("error") or {}).get("message") or "provider error"))
                break
            lines.append(line)
            if on_step is None or not any(k in head for k in _PROGRESS_TYPES):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            doing = _doing(event) if isinstance(event, dict) else None
            if doing is not None:
                steps += event.get("type") == "tool_execution_start"
                on_step(doing, steps)
        text, spent, failure = parse_events("".join(lines))
        return text, spent, failure or error, steps

    async def close(self) -> None:
        """结束进程（连同它起的子进程），等它真正退出再删工作目录。"""
        self.ready.cancel()
        await asyncio.wait([self.ready])  # 等启动真正停下（不抛它的异常），免得进程刚起来没人管
        if not self.ready.cancelled():
            self.ready.exception()  # 标记已取走，免得事件循环报「异常没人取」
        if self.proc is not None:
            _kill(self.proc)
            await self.proc.wait()
            # 管道都读到头，子进程的传输层才会自己关掉（否则事件循环关闭后会报「管道没关」）
            self.proc.stdin.close()
            await self.proc.stdout.read()
            if self._stderr_task is not None:
                await asyncio.wait([self._stderr_task])
        shutil.rmtree(self.workdir, ignore_errors=True)


def _is_frame(line: str, kind: str) -> bool:
    if f'"{kind}"' not in line[:120]:
        return False
    try:
        return json.loads(line).get("type") == kind
    except (ValueError, AttributeError):
        return False


# 空闲的常驻 omp：每个研究员模型最多一个（可能还在启动）。绑定事件循环，换了循环（如测试）就把旧的进程杀掉丢掉
_idle: dict[str, _Omp] = {}
_idle_loop: asyncio.AbstractEventLoop | None = None
_background: set[asyncio.Task] = set()


def _pool() -> dict[str, _Omp]:
    global _idle_loop
    loop = asyncio.get_running_loop()
    if _idle_loop is not loop:
        for omp in _idle.values():
            if omp.proc is not None:
                _kill(omp.proc)
            shutil.rmtree(omp.workdir, ignore_errors=True)
        _idle.clear()
        _idle_loop = loop
    return _idle


def warm_up() -> None:
    """给每个研究员模型启动一个常驻 omp（RESEARCH_WARM 关闭时不做），启动时和每次用掉一个之后调用。"""
    if not config.RESEARCH_WARM:
        return
    pool = _pool()
    for model in config.RESEARCH_MODELS:
        if model not in pool:
            pool[model] = _Omp(model)
            task = asyncio.create_task(_watch_start(pool[model]))
            _background.add(task)
            task.add_done_callback(_background.discard)


async def _watch_start(omp: _Omp) -> None:
    """预热失败（比如 key 配错）时丢掉这个进程，等真有任务时再冷启动、把错误报给主模型。"""
    try:
        await omp.ready
    except Exception as e:
        logger.warning("预热 omp %s 失败：%s", omp.model, e)
        if _pool().get(omp.model) is omp:
            del _idle[omp.model]
        await omp.close()


async def close_pool() -> None:
    """结束所有空闲的常驻 omp（应用退出、测试收尾时用）。"""
    pool = _pool()
    idle = list(pool.values())
    pool.clear()
    for omp in idle:
        await omp.close()


async def _acquire(model: str) -> _Omp:
    """取一个这个模型的 omp：有预热好（或正在预热）的就用它，没有就当场冷启动。"""
    omp = _pool().pop(model, None)
    if omp is None or (omp.ready.done() and not omp.alive):
        if omp is not None:
            await omp.close()
        omp = _Omp(model)
    try:
        await omp.ready
    except BaseException:
        await asyncio.shield(omp.close())
        raise
    return omp


async def _release(omp: _Omp, reusable: bool) -> None:
    pool = _pool()
    if reusable and config.RESEARCH_WARM and omp.alive and omp.uses < RECYCLE_AFTER \
            and omp.model in config.RESEARCH_MODELS and omp.model not in pool:
        pool[omp.model] = omp
        return
    await omp.close()
    warm_up()  # 补一个新的，下一个任务不用等冷启动


async def run_researcher(model: str, task: str, on_step: Step | None = None) -> Report:
    """跑一个研究员。永不抛异常（取消除外）：失败时 Report.error 说明原因。
    omp 边跑边一行一个事件地输出；每开始一次搜索或读网页，on_step 收到（在做什么, 第几步）。"""
    t0 = time.monotonic()
    report = Report(model)
    steps, failure, omp, reusable = 0, "", None, False
    try:
        omp = await _acquire(model)
        async with asyncio.timeout(config.RESEARCH_TIMEOUT):
            report.text, report.usage, failure, steps = await omp.run(task, on_step)
        reusable = True
    except TimeoutError:
        report.error = "timed out"
    except (OSError, RuntimeError) as e:
        report.error = (str(e) or type(e).__name__)[:200]
    finally:
        # 回复被取消、超时或出错：进程整个丢掉，不留下还在花钱的任务
        if omp is not None:
            await asyncio.shield(_release(omp, reusable))
    report.secs = time.monotonic() - t0
    if not report.text and not report.error:
        report.error = failure or "no answer"
    logger.info("研究员 %s %s：%.0fs，%d 步，%d 字，token 输入 %s / 输出 %s", model,
                "完成" if report.text else f"失败（{report.error}）", report.secs, steps, len(report.text),
                logs.tokens(report.tokens_in), logs.tokens(report.usage.get("output", 0)))
    usage.record("research", model, report.tokens_in, report.usage.get("output", 0), report.secs, ok=bool(report.text))
    return report


def _backup(i: int) -> str | None:
    """第 i 个研究员的备选模型：RESEARCH_FALLBACK_MODELS 按位置对应，只写一个就所有人共用。"""
    backups = config.RESEARCH_FALLBACK_MODELS
    if not backups:
        return None
    return backups[i] if i < len(backups) else backups[-1]


class _Board:
    """进度里调研那一行下面的明细：每个研究员一行，写明在用哪个模型、在做什么、第几步。"""

    def __init__(self, models: list[str]):
        self.rows = [t("research_row", label=_label(i), model=_name(m), doing=t("research_doing_start"))
                     for i, m in enumerate(models)]
        show_detail(self.rows)

    def set(self, i: int, text: str) -> None:
        self.rows[i] = text
        show_detail(self.rows)

    def stepper(self, i: int, model: str, backup: bool) -> Step:
        key = "research_row_backup" if backup else "research_row"

        def step(doing: str, n: int) -> None:
            if n:
                doing = t("research_step", doing=doing, n=n)
            self.set(i, t(key, label=_label(i), model=_name(model), doing=doing))
        return step


def _label(i: int) -> str:
    return chr(ord("A") + i)


def _name(model: str) -> str:
    return model.rsplit("/", 1)[-1]


async def run_with_backup(i: int, model: str, task: str, board: _Board | None = None) -> Report:
    """跑第 i 个研究员；失败（超时除外：时间已经花完了，再跑备选就是让群友再等一整轮）就用它的备选模型再跑一次。
    几个研究员各自的主→备是先后进行的，研究员之间同时进行。"""
    report = await run_researcher(model, task, board.stepper(i, model, False) if board else None)
    backup = _backup(i)
    if not report.text and backup and backup != model and report.error != "timed out":
        logger.info("研究员 %s 失败（%s），改用备选 %s", model, report.error, backup)
        step = board.stepper(i, backup, True) if board else None
        if step is not None:
            step(t("research_doing_start"), 0)
        second = await run_researcher(backup, task, step)
        if not second.text:
            second.error = f"{model}: {report.error}; {backup}: {second.error}"
        report = second
    if board is not None:
        reason = t("research_reason_timeout") if report.error.endswith("timed out") else report.error.rsplit(": ", 1)[-1][:40]
        board.set(i, t("research_row_done" if report.text else "research_row_failed", label=_label(i),
                       model=_name(report.model), reason=reason))
    return report


_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _slot() -> asyncio.Semaphore:
    """限制同时进行的调研任务数；信号量绑定事件循环，换了循环（如测试）就重建。"""
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(max(1, config.RESEARCH_CONCURRENCY)))
    return _slots[1]


class ResearchTool(Tool):
    name = "research"
    description = (
        "Hand one research task to independent research agents (different models) that search the web, read the "
        "actual pages, and return verified facts with sources. For anything serious (why something happened, money, "
        "health, law, politics, safety), origins, true-or-false checks, and unfamiliar memes, even if you think you know."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The full research task with background: the exact wording in question, which "
                               "community or platform it comes from, and what to find out. The agents can't see the chat.",
            }
        },
        "required": ["task"],
    }

    def enabled(self) -> bool:
        return bool(config.RESEARCH_MODELS)

    def prompt_note(self) -> str:
        return t("research_system_prompt")

    def label(self, args: dict) -> str:
        return t("tool_research", task=str(args.get("task", "")).strip()[:48] or "?")

    async def run(self, args: dict, chat_id: int | None) -> Outcome:
        task = str(args.get("task", "")).strip()
        if not task:
            return failed(t("research_failed", reason="empty task"))
        async with _slot():
            board = _Board(config.RESEARCH_MODELS)
            reports = await asyncio.gather(*(run_with_backup(i, m, task, board)
                                             for i, m in enumerate(config.RESEARCH_MODELS)))
        blocks = []
        for i, r in enumerate(reports):
            label, model = _label(i), _name(r.model)
            if r.text:
                blocks.append(t("research_report", label=label, model=model, secs=r.secs) + "\n"
                              + r.text[:REPORT_CHARS])
            else:
                blocks.append(t("research_member_failed", label=label, model=model, reason=r.error))
        done = sum(1 for r in reports if r.text)
        if not done:
            return failed(t("research_failed", reason="; ".join(f"{r.model}: {r.error}" for r in reports)))
        fails = len(reports) - done
        summary = t("res_reports", n=done) + (t("res_fail_suffix", n=fails) if fails else "")
        return Outcome("\n\n".join(blocks), True, summary)
