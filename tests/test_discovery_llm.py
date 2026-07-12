"""Phase 3 — the LLM discoverer (with an injected fake caller; no CLI spawned)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from bot.clock import IST
from bot.discovery.discover import (
    _extract_json,
    build_prompt,
    discover_and_register,
)
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


def _caller_returning(strategies):
    def caller(prompt):
        return "Here you go:\n```json\n" + json.dumps({"strategies": strategies}) + "\n```"
    return caller


# --- prompt + parsing --------------------------------------------------------

def test_prompt_bakes_in_intraday_rules_and_vocab():
    p = build_prompt("DISCOVERED_EQ", ["close > vwap"], 5)
    assert "INTRADAY ONLY" in p
    assert "rsi14" in p and "or_high" in p
    assert "Already-registered" in p and "close > vwap" in p


def test_opt_prompt_excludes_volume_fields():
    p = build_prompt("DISCOVERED_OPT", [], 5)
    # volume fields must not appear as allowed-field bullets (they're None on
    # indices); the rules text may still mention they're unavailable.
    assert "- rvol" not in p and "- vwap:" not in p and "- vwap_dist_pct" not in p
    assert "- or_high:" in p


def test_extract_json_tolerates_fences_and_prose():
    obj = _extract_json("blah\n```json\n{\"strategies\": [{\"name\": \"x\"}]}\n```\ndone")
    assert obj["strategies"][0]["name"] == "x"


# --- discover_and_register end to end ---------------------------------------

def test_discover_registers_passing_and_rejects_failing(mem_db):
    caller = _caller_returning([
        {"name": "up_bias", "entry_expr": "close > day_open", "side": "LONG",
         "min_reward_risk": 1.5, "rationale": "trend persistence"},
        {"name": "down_bias", "entry_expr": "close < day_open", "side": "LONG",
         "min_reward_risk": 1.5, "rationale": "will lose on rising data"},
    ])
    report = discover_and_register("DISCOVERED_EQ", caller=caller,
                                   histories=_sessions(30))
    assert report.proposed == 2
    assert "up_bias" in report.registered
    assert any(name == "down_bias" for name, _ in report.rejected)
    assert [s.name for s in load_active_specs("DISCOVERED_EQ")] == ["up_bias"]


def test_discover_rejects_unknown_indicator_naming_it(mem_db):
    caller = _caller_returning([
        {"name": "needs_st", "entry_expr": "close > supertrend", "side": "LONG",
         "min_reward_risk": 1.5, "rationale": "supertrend flip"},
    ])
    report = discover_and_register("DISCOVERED_EQ", caller=caller,
                                   histories=_sessions(30))
    assert report.registered == []
    name, reason = report.rejected[0]
    assert name == "needs_st" and "supertrend" in reason


def test_discover_rejects_malicious_expr_safely(mem_db):
    caller = _caller_returning([
        {"name": "evil", "entry_expr": "__import__('os').system('x')", "side": "LONG",
         "min_reward_risk": 1.5, "rationale": "nope"},
    ])
    report = discover_and_register("DISCOVERED_EQ", caller=caller,
                                   histories=_sessions(30))
    assert report.registered == []
    assert report.rejected and load_active_specs("DISCOVERED_EQ") == []


def test_discover_survives_caller_failure(mem_db):
    def boom(prompt):
        raise RuntimeError("claude CLI not found")
    report = discover_and_register("DISCOVERED_EQ", caller=boom, histories=_sessions(30))
    assert report.raw_ok is False and report.proposed == 0
