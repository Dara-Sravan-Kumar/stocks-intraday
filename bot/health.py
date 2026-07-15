"""Run-health classification: turn the state of a finished session into a list
of {"kind", "detail"} failures for a Discord alert. Pure string/flag matching —
no I/O — so it's easy to test and can never break a run.

Intraday-relevant categories:
  * LIVE DATA FEED   — the real-time Fyers feed degraded to a fallback or
    aborted. CRITICAL: an intraday bot blind to fresh bars can't trade at all.
    For this bot a stale morning Fyers login is the usual root cause, so the
    remedy (run the daily login) is named in the detail.
  * LLM / DISCOVERY  — the reflective R&D loop's Claude CLI calls (post-mortem
    or discovery) produced nothing usable (missing on PATH / failed / timed out).

There is no news / LLM-sentiment data source in this bot, so that swing-bot
category is intentionally omitted.
"""
from __future__ import annotations


def feed_failures(feed_source: str | None, *, require_fyers: bool) -> list[dict]:
    """A LIVE DATA FEED failure when a run that REQUIRES the real Fyers feed
    (production paper / live) ran on a fallback or aborted source instead."""
    if not require_fyers:
        return []   # --feed yf / dhan / replay dev runs intentionally aren't Fyers
    src = (feed_source or "").strip()
    low = src.lower()
    if low.startswith("fyers-ws") and "degraded" not in low and "aborted" not in low:
        return []   # healthy real feed
    if "aborted" in low or "failed" in low:
        detail = (f"Fyers feed ABORTED ({src}) — no fresh bars arrived, so the "
                  "session could not trade. Run the morning Fyers login "
                  "(python -m bot.fyers_auth) before 08:45.")
    else:
        detail = (f"Live feed ran on fallback '{src or 'unknown'}' instead of the "
                  "Fyers websocket — bars are laggy and the paper book was FROZEN. "
                  "The daily Fyers login is likely stale; run it (one login covers "
                  "all 3 bots).")
    return [{"kind": "LIVE DATA FEED / LOGIN", "detail": detail}]


def discovery_failures(discovery_report: dict | None) -> list[dict]:
    """LLM/discovery failures already collected by run_daily_discovery (it knows
    its own post-mortem/discovery outcomes). Skipped runs carry none."""
    if not discovery_report:
        return []
    return list(discovery_report.get("failures", []))


def collect_failures(*, feed_source: str | None, require_fyers: bool,
                     discovery_report: dict | None) -> list[dict]:
    """Compose the full failure list for the EOD health alert."""
    return (feed_failures(feed_source, require_fyers=require_fyers)
            + discovery_failures(discovery_report))
