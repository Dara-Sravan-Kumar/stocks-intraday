"""Task 1a — the daily trade post-mortem (injected caller; no CLI spawned)."""
from __future__ import annotations

import json

import config
from bot import db
from bot.discovery import postmortem


def _record(i: int, net: float, *, variant="orb", exit_reason="TARGET"):
    entry = f"2026-07-06T10:{i:02d}:00+05:30"
    exit_ = f"2026-07-06T10:{i + 20:02d}:00+05:30"
    db.record_trade(
        run_id=None, mode="PAPER", strategy="orb", variant_key=variant,
        symbol="RELIANCE", side="LONG", qty=10,
        entry_ts=entry, entry_price=100.0, exit_ts=exit_, exit_price=100.0 + net / 10,
        gross_pnl=net, costs=5.0, net_pnl=net, r_multiple=(net / 500.0),
        planned_stop=99.5, planned_target=101.0, exit_reason=exit_reason,
        feed_source="fyers-ws",
    )


def _caller(lessons, diagnosis="stops too tight"):
    def caller(prompt):
        assert "INTRADAY" in prompt and "square-off" in prompt.lower()
        return "```json\n" + json.dumps({"lessons": lessons, "diagnosis": diagnosis}) + "\n```"
    return caller


def test_returns_parsed_lessons(mem_db):
    for i in range(8):
        _record(i, 300.0 if i % 2 else -200.0)
    out = postmortem.analyze_recent_trades(
        mode="PAPER", caller=_caller(["widen stops", "skip 09:15-09:45"]))
    assert out["reviewed"] == 8 and out["ok"] is True
    assert out["lessons"] == ["widen stops", "skip 09:15-09:45"]
    assert out["diagnosis"] == "stops too tight"


def test_too_few_trades_is_empty(mem_db):
    for i in range(3):
        _record(i, 100.0)
    out = postmortem.analyze_recent_trades(mode="PAPER", caller=_caller(["x"]))
    assert out == {"reviewed": 0, "lessons": [], "diagnosis": "", "ok": True}


def test_caller_failure_flags_not_ok(mem_db):
    for i in range(8):
        _record(i, 100.0)

    def boom(prompt):
        raise RuntimeError("claude CLI not found")

    out = postmortem.analyze_recent_trades(mode="PAPER", caller=boom)
    assert out["reviewed"] == 8 and out["ok"] is False and out["lessons"] == []


def test_disabled_returns_empty(mem_db, monkeypatch):
    monkeypatch.setattr(config, "POSTMORTEM_ENABLED", False)
    for i in range(8):
        _record(i, 100.0)
    out = postmortem.analyze_recent_trades(mode="PAPER", caller=_caller(["x"]))
    assert out["reviewed"] == 0 and out["lessons"] == []


def test_lessons_capped_at_six(mem_db):
    for i in range(8):
        _record(i, 100.0)
    out = postmortem.analyze_recent_trades(
        mode="PAPER", caller=_caller([f"l{i}" for i in range(10)]))
    assert len(out["lessons"]) == 6
