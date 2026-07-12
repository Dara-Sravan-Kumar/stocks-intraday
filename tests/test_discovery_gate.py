"""Phase 2 — backtest gate + DISCOVERED channels."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from bot.clock import IST
from bot.discovery.gate import _Session, backtest_gate
from bot.discovery.registry import load_active_specs, register_spec, retire_pass
from bot.discovery.spec import StrategySpec
from bot.state import MarketState
from tests.conftest import bar_series

BASE_DAY = date(2026, 1, 6)


def _session(day: date, up: bool, n: int = 75) -> _Session:
    start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    step = 0.05 if up else -0.05
    closes = [100.0 + i * step for i in range(n)]
    return _Session(day, bar_series("X", start, closes))


def _sessions(n_up: int, n_down: int = 0) -> list[_Session]:
    out = []
    d = BASE_DAY
    for _ in range(n_up):
        out.append(_session(d, up=True))
        d += timedelta(days=1)
    for _ in range(n_down):
        out.append(_session(d, up=False))
        d += timedelta(days=1)
    return out


UP_SPEC = StrategySpec(name="up_bias", entry_expr="close > day_open",
                       channel="DISCOVERED_EQ", side="LONG", min_reward_risk=1.5)


# --- the gate ---------------------------------------------------------------

def test_gate_passes_a_predictive_spec():
    res = backtest_gate(UP_SPEC, histories={"X": _sessions(30)})
    assert res.passed, res.reason
    assert res.oos_trades >= 8
    assert res.is_net_pct > 0


def test_gate_fails_out_of_sample_even_if_in_sample_wins():
    # rising IN-SAMPLE (LONG wins) but falling OUT-OF-SAMPLE (LONG loses):
    # the recent OOS window is the real test and it rejects the spec.
    res = backtest_gate(UP_SPEC, histories={"X": _sessions(20, n_down=12)})
    assert not res.passed
    assert "OOS" in res.reason
    assert res.is_net_pct > 0    # in-sample looked fine — that's the trap


def test_gate_fails_on_too_few_trades():
    # a spec that never fires -> no OOS trades -> rejected
    never = StrategySpec(name="never", entry_expr="close < day_open and close > day_high",
                         channel="DISCOVERED_EQ")
    res = backtest_gate(never, histories={"X": _sessions(30)})
    assert not res.passed
    assert res.oos_trades == 0


def test_gate_rejects_non_intraday_before_replay():
    swing = StrategySpec(name="swinger", entry_expr="close > day_open", horizon="SWING")
    res = backtest_gate(swing, histories={"X": _sessions(30)})
    assert not res.passed and "invalid" in res.reason


# --- registration -----------------------------------------------------------

def test_register_spec_full_flow(mem_db):
    res = register_spec(UP_SPEC, histories={"X": _sessions(30)})
    assert res.registered, res.reason
    active = load_active_specs("DISCOVERED_EQ")
    assert [s.name for s in active] == ["up_bias"]
    assert active[0].entry_expr == "close > day_open"


def test_register_rejects_duplicate_expr(mem_db):
    assert register_spec(UP_SPEC, histories={"X": _sessions(30)}).registered
    dup = StrategySpec(name="up_bias_2", entry_expr="close  >  day_open",  # whitespace variant
                       channel="DISCOVERED_EQ")
    res = register_spec(dup, histories={"X": _sessions(30)})
    assert not res.registered and "duplicate" in res.reason


def test_register_rejects_bad_horizon(mem_db):
    swing = StrategySpec(name="swinger", entry_expr="close > day_open", horizon="SWING")
    res = register_spec(swing, histories={"X": _sessions(30)})
    assert not res.registered and "invalid" in res.reason


def test_register_honors_fleet_cap(mem_db, monkeypatch):
    import config
    monkeypatch.setattr(config, "DISCOVERED_FLEET_MAX", 1)
    assert register_spec(UP_SPEC, histories={"X": _sessions(30)}).registered
    other = StrategySpec(name="up2", entry_expr="close > ema20",
                         channel="DISCOVERED_EQ")
    res = register_spec(other, histories={"X": _sessions(30)})
    assert not res.registered and "fleet full" in res.reason


def test_retire_pass_drops_net_negative_variant(mem_db):
    from bot import db
    assert register_spec(UP_SPEC, histories={"X": _sessions(30)}).registered
    # simulate a net-negative forward-paper ledger for this variant
    for i in range(16):
        db.record_trade(
            run_id=None, mode="PAPER", strategy="DISCOVERED_EQ",
            variant_key="up_bias", symbol="RELIANCE", side="LONG", qty=1,
            entry_ts="2026-07-06T10:00:00", entry_price=100.0,
            exit_ts="2026-07-06T10:30:00", exit_price=99.0,
            gross_pnl=-100.0, costs=5.0, net_pnl=-105.0, r_multiple=-1.0,
            planned_stop=99.0, planned_target=103.0, exit_reason="STOP",
        )
    retired = retire_pass("DISCOVERED_EQ")
    assert retired == ["up_bias"]
    assert load_active_specs("DISCOVERED_EQ") == []


# --- the equity channel emits signals ---------------------------------------

def test_discovered_equity_emits_signal_for_matching_spec():
    from bot.strategies.discovered import DiscoveredEquity

    strat = DiscoveredEquity([UP_SPEC])
    strat.on_session_start()
    market = MarketState(["RELIANCE"])
    st = market.get("RELIANCE")
    start = datetime(2026, 7, 6, 9, 15, tzinfo=IST)
    for bar in bar_series("RELIANCE", start, [100.0 + i * 0.05 for i in range(40)]):
        st.on_bar_1m(bar)
    now = datetime(2026, 7, 6, 10, 0, tzinfo=IST)

    sigs = strat.on_bar_5m(st, market, now)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.strategy == "DISCOVERED_EQ" and sig.variant == "up_bias"
    assert sig.side == "LONG" and sig.stop < st.last_price


def test_discovered_equity_respects_entry_deadline():
    from bot.strategies.discovered import DiscoveredEquity

    strat = DiscoveredEquity([UP_SPEC])
    strat.on_session_start()
    market = MarketState(["RELIANCE"])
    st = market.get("RELIANCE")
    start = datetime(2026, 7, 6, 9, 15, tzinfo=IST)
    for bar in bar_series("RELIANCE", start, [100.0 + i * 0.05 for i in range(40)]):
        st.on_bar_1m(bar)
    late = datetime(2026, 7, 6, 14, 0, tzinfo=IST)   # past DISCOVERED_ENTRY_DEADLINE
    assert strat.on_bar_5m(st, market, late) == []
