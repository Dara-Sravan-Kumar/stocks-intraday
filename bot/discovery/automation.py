"""Feature 4 — daily automation. Discovery (LLM) and mixing (genetic) are
expensive, so they run ONCE a day, folded into the tail of a session AFTER
orders and reporting are out. Every step is try-guarded: a discovery failure
can never break the trading run. New specs are live the next session.

The trading / retiring / capital-weighting of already-registered specs happens
every run automatically — they're just another channel in the fleet.
"""
from __future__ import annotations

import logging

import config
from bot import clock, db

log = logging.getLogger(__name__)

CHANNELS = ("DISCOVERED_EQ", "DISCOVERED_OPT")
# Each discovered channel forward-trades in a distinct paper book; the
# performance digest fed into its discovery is read from that book's ledger.
CHANNEL_MODE = {"DISCOVERED_EQ": "PAPER", "DISCOVERED_OPT": "PAPER-OPT"}
_KV_LAST_RUN = "discovery_last_date"


def already_ran_today() -> bool:
    return db.kv_get(_KV_LAST_RUN) == clock.now_ist().date().isoformat()


def run_daily_discovery(*, force: bool = False, caller=None,
                        histories=None) -> dict:
    """Reflective R&D, once per calendar day: post-mortem -> discovery (seeded
    with the post-mortem lessons + a live-performance digest) -> genetic mixer
    -> retire, across both channels. Returns a summary dict (including a
    `failures` list for the health alert); never raises (each step is guarded)."""
    if not getattr(config, "DISCOVERY_ENABLED", False):
        return {"skipped": "discovery disabled"}
    if not force and already_ran_today():
        return {"skipped": "already ran today"}

    today = clock.now_ist().date().isoformat()
    summary: dict = {"date": today, "channels": {}, "failures": []}

    # 1) Trade post-mortem first — its lessons steer the day's discovery.
    lessons: list[str] = []
    try:
        from bot.discovery import postmortem
        pm = postmortem.analyze_recent_trades(caller=caller)
        summary["postmortem"] = {"reviewed": pm["reviewed"],
                                 "diagnosis": pm["diagnosis"],
                                 "lessons": pm["lessons"]}
        lessons = pm["lessons"]
        if not pm["ok"]:
            summary["failures"].append(
                {"kind": "LLM / POST-MORTEM",
                 "detail": f"Post-mortem reviewed {pm['reviewed']} trades but the "
                           "Claude CLI returned no usable diagnosis (missing/"
                           "failed/timed out)."})
    except Exception as exc:  # noqa: BLE001
        log.warning("post-mortem errored: %s", exc)
        summary["postmortem"] = {"error": str(exc)}

    for channel in CHANNELS:
        ch: dict = {}
        if getattr(config, "DISCOVERY_LLM_ENABLED", False):
            try:
                from bot.discovery.discover import (
                    _performance_digest, discover_and_register)
                perf = _performance_digest(CHANNEL_MODE.get(channel, "PAPER"))
                rep = discover_and_register(channel, caller=caller,
                                            histories=histories,
                                            lessons=lessons, performance=perf)
                ch["discovered"] = rep.summary()
                if not rep.raw_ok:
                    summary["failures"].append(
                        {"kind": "LLM / DISCOVERY",
                         "detail": f"Discovery({channel}) got no usable proposals "
                                   "from the Claude CLI (missing/failed/timed out)."})
            except Exception as exc:  # noqa: BLE001
                log.warning("discovery(%s) errored: %s", channel, exc)
                ch["discover_error"] = str(exc)
                summary["failures"].append(
                    {"kind": "LLM / DISCOVERY",
                     "detail": f"Discovery({channel}) raised: {exc}"})

        if getattr(config, "MIXER_ENABLED", False):
            try:
                from bot.discovery.mixer import breed
                mrep = breed(channel, histories=histories)
                ch["mixed"] = mrep.summary()
            except Exception as exc:  # noqa: BLE001
                log.warning("mixer(%s) errored: %s", channel, exc)
                ch["mix_error"] = str(exc)

        try:
            from bot.discovery.registry import retire_pass
            ch["retired"] = retire_pass(channel)
        except Exception as exc:  # noqa: BLE001
            log.warning("retire(%s) errored: %s", channel, exc)
            ch["retire_error"] = str(exc)

        summary["channels"][channel] = ch

    db.kv_set(_KV_LAST_RUN, today)
    log.info("daily discovery complete: %s", summary)
    return summary
