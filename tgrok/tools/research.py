"""research：把调研任务同时交给几个 omp 研究员（不同模型），各自的报告一起交给主模型判断。

每个研究员是一个 omp 无界面进程（omp -p）：只开放 web_search 和 read，加载 research_hook.ts
把 read 限制在公网网页，在空的临时目录里运行，超时或回复被取消时连同子进程一起结束。
"""

import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..i18n import t
from . import Outcome, Tool, failed

logger = logging.getLogger(__name__)

HOOK = Path(__file__).with_name("research_hook.ts")
REPORT_CHARS = 6000  # 单份报告回灌给主模型的字数上限
KILL_GRACE = 20  # omp 自己的 --max-time 之外再等多少秒才强杀

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
    usage: dict = field(default_factory=dict)


def _command(model: str, task: str) -> list[str]:
    return [
        *shlex.split(config.OMP_BIN, posix=os.name != "nt"),
        "-p", "--mode", "json", "--no-session", "--no-title", "--no-skills", "--no-rules", "--no-extensions",
        "--no-lsp", "--tools", "web_search,read", "--hook", str(HOOK), "--model", model,
        "--thinking", config.RESEARCH_THINKING, "--max-time", str(config.RESEARCH_TIMEOUT),
        "--system-prompt", SYSTEM_PROMPT, "--", task,
    ]


def parse_events(stdout: str) -> tuple[str, dict]:
    """omp --mode json 的事件流 → (最后一条回答的正文, 累计 token 用量)。"""
    text, usage = "", {}
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
    return text.strip(), usage


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


async def run_researcher(model: str, task: str) -> Report:
    """跑一个研究员。永不抛异常（取消除外）：失败时 Report.error 说明原因。"""
    workdir = tempfile.mkdtemp(prefix="tgrok-research-")
    t0 = time.monotonic()
    report = Report(model)
    try:
        proc = await asyncio.create_subprocess_exec(
            *_command(model, task), cwd=workdir, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=os.name == "posix")
    except OSError as e:
        report.error = f"cannot start omp ({type(e).__name__})"
        shutil.rmtree(workdir, ignore_errors=True)
        return report
    try:
        out, err = await asyncio.wait_for(proc.communicate(), config.RESEARCH_TIMEOUT + KILL_GRACE)
    except asyncio.TimeoutError:
        _kill(proc)
        await proc.wait()
        report.error = "timed out"
        out = err = b""
    except BaseException:
        # 回复被取消：不留下还在花钱的进程；等它真正退出再删工作目录、关管道
        _kill(proc)
        await asyncio.shield(proc.wait())
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    report.secs = time.monotonic() - t0
    report.text, report.usage = parse_events(out.decode("utf-8", "replace"))
    if not report.text and not report.error:
        tail = err.decode("utf-8", "replace").strip().splitlines()
        report.error = (tail[-1][:200] if tail else "") or f"no answer (exit code {proc.returncode})"
    logger.info("研究员 %s %s：%.0fs，%d 字，token %s", model, "完成" if report.text else f"失败（{report.error}）",
                report.secs, len(report.text), report.usage)
    return report


def _backup(i: int) -> str | None:
    """第 i 个研究员的备选模型：RESEARCH_FALLBACK_MODELS 按位置对应，只写一个就所有人共用。"""
    backups = config.RESEARCH_FALLBACK_MODELS
    if not backups:
        return None
    return backups[i] if i < len(backups) else backups[-1]


async def run_with_backup(i: int, model: str, task: str) -> Report:
    """跑第 i 个研究员；失败（超时除外：时间已经花完了）就用它的备选模型再跑一次。"""
    report = await run_researcher(model, task)
    backup = _backup(i)
    if report.text or not backup or backup == model or report.error == "timed out":
        return report
    logger.info("研究员 %s 失败（%s），改用备选 %s", model, report.error, backup)
    second = await run_researcher(backup, task)
    if not second.text:
        second.error = f"{model}: {report.error}; {backup}: {second.error}"
    return second


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
        "Hand one research task to independent research agents (different models) that search the web and read "
        "the actual pages, and get all their reports back to compare. Slow (30 s to 2 min): use it only when an "
        "answer needs real checking, such as unknown memes or niche jargon, disputed claims, or exact numbers and dates."
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
            reports = await asyncio.gather(*(run_with_backup(i, m, task) for i, m in enumerate(config.RESEARCH_MODELS)))
        blocks = []
        for i, r in enumerate(reports):
            label, model = chr(ord("A") + i), r.model.rsplit("/", 1)[-1]
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
