"""Feature 4 — daily automation: once/day, guarded, idempotent."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from bot import clock, db
from bot.clock import IST
from bot.discovery.automation import already_ran_today, run_daily_discovery
from bot.discovery.gate import _Session
from bot.discovery.registry import load_active_specs
from tests.conftest import bar_series


def _sessions(n_up: int):
    out, d = [], date(2026, 1, 6)
    for _ in range(n_up):
        start = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
        out.append(_Session(d, bar_series("X", start, [100.0 + i * 0.05 for i in range(75)])))
        d += timedelta(days=1)
    return {"X": out}


def _caller(strategies):
    import json
    return lambda prompt: json.dumps({"strategies": strategies})


def test_runs_once_per_day_and_sets_guard(mem_db):
    caller = _caller([
        {"name": "up_bias", "entry_expr": "close > day_open", "side": "LONG",
         "min_reward_risk": 1.5, "rationale": "trend"},
    ])
    assert not already_ran_today()
    rep = run_daily_discovery(caller=caller, histories=_sessions(30))
    assert "skipped" not in rep
    assert already_ran_today()
    # up_bias registered via the EQ channel (OPT channel gets its own pass)
    assert "up_bias" in [s.name for s in load_active_specs("DISCOVERED_EQ")]

    # second call the same day is a no-op
    rep2 = run_daily_discovery(caller=caller, histories=_sessions(30))
    assert rep2["skipped"] == "already ran today"


def test_force_reruns(mem_db):
    db.kv_set("discovery_last_date", clock.now_ist().date().isoformat())
    assert already_ran_today()
    rep = run_daily_discovery(force=True, caller=_caller([]), histories=_sessions(30))
    assert "skipped" not in rep


def test_survives_a_failing_llm_caller(mem_db):
    def boom(prompt):
        raise RuntimeError("claude CLI missing")
    rep = run_daily_discovery(caller=boom, histories=_sessions(30))
    # discovery step failed gracefully but the pass still completed + set the guard
    assert "skipped" not in rep and already_ran_today()
    for channel, ch in rep["channels"].items():
        assert "retired" in ch   # retire pass still ran


def test_disabled_short_circuits(mem_db, monkeypatch):
    import config
    monkeypatch.setattr(config, "DISCOVERY_ENABLED", False)
    assert run_daily_discovery()["skipped"] == "discovery disabled"
