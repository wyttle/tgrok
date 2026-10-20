"""模型用量：每次模型调用记一行（功能、模型、token、耗时、成败），/stats 按功能和模型汇总。

功能：reply = 回复（主模型和备用模型的每一轮），research = omp 研究员，search = Gemini grounding 搜索，
media = 看视频、听语音，memory = 记忆整理。token 以各接口报的为准，没报的记 0。
同一请求里的调用同时累加到 logs 的请求上下文，回复完成时的汇总日志行带上合计。
"""

import time
from datetime import datetime

from . import config, logs
from .db import db, schema
from .i18n import phrases, t

KEEP_DAYS = 90

schema("""
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    feature TEXT NOT NULL,
    model TEXT NOT NULL,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    secs REAL NOT NULL,
    ok INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts);
""")


def record(feature: str, model: str, tokens_in: int, tokens_out: int, secs: float, ok: bool = True) -> None:
    now = time.time()
    conn = db()
    conn.execute("INSERT INTO usage(ts, feature, model, tokens_in, tokens_out, secs, ok) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (now, feature, model, tokens_in, tokens_out, secs, int(ok)))
    conn.execute("DELETE FROM usage WHERE ts < ?", (now - KEEP_DAYS * 86400,))
    req = logs.current()
    if req is not None:
        req.tokens_in += tokens_in
        req.tokens_out += tokens_out


def summary(since: float) -> list[dict]:
    """since 以来按（功能, 模型）汇总，调用多的在前。"""
    rows = db().execute(
        "SELECT feature, model, COUNT(*) AS calls, SUM(1 - ok) AS fails, SUM(tokens_in) AS tin, "
        "SUM(tokens_out) AS tout, AVG(secs) AS secs FROM usage WHERE ts >= ? "
        "GROUP BY feature, model ORDER BY calls DESC", (since,)).fetchall()
    return [dict(r) for r in rows]


def _section(name: str, rows: list[dict]) -> str:
    if not rows:
        return t("stats_empty", name=name)
    features = phrases("usage_features")
    head = t("stats_period", name=name, calls=sum(r["calls"] for r in rows),
             tin=logs.tokens(sum(r["tin"] for r in rows)), tout=logs.tokens(sum(r["tout"] for r in rows)))
    lines = [t("stats_line", feature=features.get(r["feature"], r["feature"]), model=r["model"], calls=r["calls"],
               fails=t("stats_fails", n=r["fails"]) if r["fails"] else "", tin=logs.tokens(r["tin"]),
               tout=logs.tokens(r["tout"]), secs=r["secs"]) for r in rows]
    return "\n".join([head] + lines)


def report(now: float | None = None) -> str:
    """/stats 的内容：今天（按 BOT_TZ 的零点算）和近 7 天。"""
    now = time.time() if now is None else now
    midnight = datetime.fromtimestamp(now, config.BOT_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    periods = phrases("stats_periods")
    return "\n\n".join([_section(periods["today"], summary(midnight.timestamp())),
                        _section(periods["week"], summary(now - 7 * 86400)),
                        t("stats_note")])
