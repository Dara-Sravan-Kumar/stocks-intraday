"""Reflective R&D — the daily trade post-mortem.

After the market closes, review the most-recent CLOSED intraday trades via the
Claude CLI (`claude -p`, subscription-billed, NOT the paid API) and distil the
book's SYSTEMATIC failure modes into a handful of actionable lessons. Those
lessons are fed into the next day's strategy discovery so proposals target what
is actually bleeding on this book.

Everything here degrades gracefully: disabled, too few trades, a missing CLI or
an unparseable reply all return the same empty shape and the caller proceeds
unchanged. It never raises — a post-mortem can never break a trading run.

The per-trade context is deliberately INTRADAY: time-of-day of entry/exit, bars
held on the 5m timeframe, R-multiple, the exit trigger, and the target/stop
distances the trade was set for — so the model can spot intraday-specific
pathologies (stops inside per-bar noise, opening-volatility whipsaw, holding
losers into the square-off, entering already-extended moves, a time window that
consistently bleeds).
"""
from __future__ import annotations

import logging
from datetime import datetime

import config
from bot import db
from bot.discovery import discover

log = logging.getLogger(__name__)


def _hhmm(ts: str | None) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M")
    except (TypeError, ValueError):
        return "?"


def _bars_held(entry_ts: str | None, exit_ts: str | None) -> str:
    try:
        mins = (datetime.fromisoformat(exit_ts)
                - datetime.fromisoformat(entry_ts)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return "?"
    if mins < 0:
        return "?"
    return f"{int(mins // max(1, config.STRATEGY_INTERVAL_MIN))}"


def _pct(a: float | None, b: float | None) -> str:
    """Signed % of a from b (e.g. stop distance from entry). '?' if unusable."""
    if not a or not b:
        return "?"
    return f"{(a - b) / b * 100.0:+.2f}%"


def _trade_line(r) -> str:
    entry = r["entry_price"]
    exit_ = r["exit_price"]
    side = r["side"]
    pnl_pct = 0.0
    if entry:
        raw = (exit_ - entry) / entry * 100.0
        pnl_pct = raw if side == "LONG" else -raw
    rmult = r["r_multiple"]
    r_s = f"{rmult:+.2f}R" if rmult is not None else "?R"
    return (f"- {r['symbol']} [{r['variant_key'] or r['strategy']}] {side} "
            f"{_hhmm(r['entry_ts'])}->{_hhmm(r['exit_ts'])} "
            f"{_bars_held(r['entry_ts'], r['exit_ts'])} bars, "
            f"pnl {pnl_pct:+.2f}% ({r_s}), exit={r['exit_reason'] or '?'} "
            f"(target {_pct(r['planned_target'], entry)} / "
            f"stop {_pct(r['planned_stop'], entry)})")


def _build_prompt(rows) -> str:
    wins = sum(1 for r in rows if (r["net_pnl"] or 0) > 0)
    losses = len(rows) - wins
    lines = [
        "You are a trading-desk risk reviewer running a post-mortem on an NSE "
        "(India) INTRADAY paper book. Every position is opened and squared off "
        "the SAME session (minutes-to-hours, flat by close) on 5-minute bars — "
        "there are NO overnight or multi-day holds.",
        "",
        f"Here are the {len(rows)} most-recent CLOSED trades ({wins} winners, "
        f"{losses} losers). Each shows the entry->exit time-of-day, bars held "
        "(5m), realized P&L % and R-multiple, the exit trigger, and the "
        "target/stop distances the trade was set for:",
        "",
    ]
    lines += [_trade_line(r) for r in rows]
    lines += [
        "",
        "Diagnose the SYSTEMATIC patterns — not one-off trades. Look hard for "
        "INTRADAY failure modes:",
        "  - stops so tight they sit inside per-bar noise and get tagged early;",
        "  - whipsaw from entering into opening volatility;",
        "  - losers held all the way into the end-of-day square-off;",
        "  - entries chasing an already-extended move;",
        "  - one setup, side, or time-of-day window that consistently bleeds;",
        "  - winners cut early vs targets that were never realistic.",
        "Turn each into a concrete, actionable lesson a strategy designer can "
        "act on (e.g. 'RSI2 scalps entered before 09:45 lose to opening whipsaw "
        "— start that window at 10:00').",
        "",
        "Respond with ONLY this JSON object, no prose, no markdown fences:",
        '{"lessons": ["<=6 short actionable lessons"], '
        '"diagnosis": "<=2 sentence summary"}',
    ]
    return "\n".join(lines)


def analyze_recent_trades(mode: str | None = None, *, caller=None,
                          lookback: int | None = None,
                          min_trades: int | None = None) -> dict:
    """Review the most-recent closed trades on `mode` and return
    {"reviewed": int, "lessons": [str], "diagnosis": str, "ok": bool}.

    Empty lessons means: disabled, too few trades, no CLI, or an unparseable
    reply — callers proceed unchanged. `ok` is False only when the LLM was asked
    but produced nothing usable (so a failure alert can flag it). `caller` is
    injectable so tests never spawn the CLI. Never raises."""
    empty = {"reviewed": 0, "lessons": [], "diagnosis": "", "ok": True}
    if not getattr(config, "POSTMORTEM_ENABLED", False):
        return empty
    mode = mode or config.POSTMORTEM_MODE
    lookback = lookback or config.POSTMORTEM_LOOKBACK_TRADES
    min_trades = min_trades if min_trades is not None else config.POSTMORTEM_MIN_TRADES
    caller = caller or discover._claude_cli

    try:
        rows = db.recent_closed_trades(mode, lookback)
    except Exception as exc:  # noqa: BLE001
        log.warning("post-mortem: could not load trades: %s", exc)
        return empty
    if len(rows) < min_trades:
        return empty

    try:
        raw = caller(_build_prompt(rows))
        parsed = discover._extract_json(raw, require_key="lessons")
    except Exception as exc:  # noqa: BLE001 — a post-mortem failure must never crash the run
        log.warning("post-mortem: no usable diagnosis from Claude CLI: %s", exc)
        return {**empty, "reviewed": len(rows), "ok": False}

    raw_lessons = parsed.get("lessons")
    lessons = ([str(x).strip() for x in raw_lessons if str(x).strip()][:6]
               if isinstance(raw_lessons, list) else [])
    diagnosis = str(parsed.get("diagnosis", "")).strip()[:300]
    log.info("post-mortem reviewed %d trades: %s", len(rows), diagnosis)
    return {"reviewed": len(rows), "lessons": lessons,
            "diagnosis": diagnosis, "ok": True}
