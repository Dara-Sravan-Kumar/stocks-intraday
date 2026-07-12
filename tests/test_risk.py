from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

import config
from bot.clock import IST
from bot.execution import LONG, Position
from bot.indicators import PrevDayLevels
from bot.risk import Approval, DayState, RiskEngine, Skip
from bot.state import MarketState, SymbolState


def ts(h=10, m=0):
    return datetime(2026, 7, 6, h, m, tzinfo=IST)


def liquid_state(symbol="X") -> SymbolState:
    st = SymbolState(symbol, PrevDayLevels(avg_1m_turnover=10_000_000))
    return st


def make_pos(strategy="orb", symbol="A") -> Position:
    return Position(strategy, symbol, LONG, 10, ts(), 100, 98, 104, 200, 98)


def approve(engine, day, *, strategy="orb", symbol="X", entry=500.0, stop=495.0,
            positions=None, equity=100_000.0, margin_used=0.0, sym_state=None):
    return engine.approve(
        strategy=strategy, symbol=symbol, entry_price=entry, stop_price=stop,
        sym_state=sym_state or liquid_state(symbol),
        open_positions=positions or [], equity=equity,
        margin_used=margin_used, day=day, now=ts(),
    )


def _expected_qty(eq, entry, stop, margin_used=0.0):
    """Mirror approve()'s equity sizing: risk, then notional cap, then margin cap."""
    qty = math.floor(eq * config.RISK_PER_TRADE_PCT / 100.0 / abs(entry - stop))
    max_notional = eq * config.MAX_NOTIONAL_PCT / 100.0
    if qty * entry > max_notional:
        qty = math.floor(max_notional / entry)
    margin_cap = eq * config.MAX_MARGIN_PCT / 100.0
    if margin_used + qty * entry / config.INTRADAY_LEVERAGE > margin_cap:
        afford = (margin_cap - margin_used) * config.INTRADAY_LEVERAGE
        qty = min(qty, math.floor(afford / entry))
    return qty


def test_sizing_formula():
    engine = RiskEngine()
    eq = 100_000
    day = DayState(start_equity=eq)
    res = approve(engine, day, entry=500.0, stop=490.0, equity=eq)
    assert isinstance(res, Approval)
    assert res.qty == _expected_qty(eq, 500.0, 490.0)
    assert res.margin == pytest.approx(res.qty * 500 / config.INTRADAY_LEVERAGE)


def test_notional_cap_reduces_qty():
    engine = RiskEngine()
    eq = 100_000
    day = DayState(start_equity=eq)
    # tight-ish stop -> large qty by risk; capped by the notional cap
    res = approve(engine, day, entry=500.0, stop=498.0, equity=eq)
    assert isinstance(res, Approval)
    assert res.qty == math.floor(eq * config.MAX_NOTIONAL_PCT / 100.0 / 500)


def test_stop_distance_gate_blocks_fee_eaten_setups():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    res = approve(engine, day, entry=500.0, stop=499.8)  # 0.04% stop distance
    assert isinstance(res, Skip) and "too tight" in res.reason


def test_margin_cap_reduces_qty():
    engine = RiskEngine()
    eq = 100_000
    day = DayState(start_equity=eq)
    margin_cap = eq * config.MAX_MARGIN_PCT / 100.0
    used = margin_cap - 500              # only ₹500 margin headroom -> margin binds below notional
    res = approve(engine, day, entry=500.0, stop=498.0, margin_used=used, equity=eq)
    assert isinstance(res, Approval)
    assert res.qty == _expected_qty(eq, 500.0, 498.0, margin_used=used)


def test_daily_halt_blocks():
    engine = RiskEngine()
    day = DayState(start_equity=100_000, halted=True, halt_reason="max daily loss")
    res = approve(engine, day)
    assert isinstance(res, Skip) and "halted" in res.reason


def test_daily_loss_breach_detection():
    engine = RiskEngine()
    eq = 100_000
    day = DayState(start_equity=eq)
    thresh = eq * (1 - config.MAX_DAILY_LOSS_PCT / 100.0)  # equity at exactly -MAX%
    assert not engine.daily_loss_breached(thresh + 100, day)
    assert engine.daily_loss_breached(thresh, day)


def test_max_concurrent_positions():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    positions = [make_pos(symbol=f"P{i}") for i in range(config.MAX_CONCURRENT_POSITIONS)]
    res = approve(engine, day, positions=positions)
    assert isinstance(res, Skip) and "concurrent" in res.reason


def test_per_strategy_limit():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    per_strat = [make_pos(strategy="orb", symbol=f"P{i}")
                 for i in range(config.MAX_POSITIONS_PER_STRATEGY)]
    res = approve(engine, day, positions=per_strat, strategy="orb")
    assert isinstance(res, Skip) and "orb" in res.reason


def test_same_strategy_cannot_double_up_on_symbol():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    res = approve(engine, day, positions=[make_pos(strategy="gap", symbol="X")],
                  symbol="X", strategy="gap")
    assert isinstance(res, Skip) and "X" in res.reason


def test_different_strategies_may_share_a_symbol():
    """The paper-test behaviour: strategies compete on the same setup."""
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    # a different strategy CAN take a symbol another strategy already holds
    res = approve(engine, day, positions=[make_pos(strategy="orb", symbol="X")],
                  symbol="X", strategy="gap")
    assert isinstance(res, Approval)
    # ...until MAX_POSITIONS_PER_SYMBOL distinct strategies are on it
    crowd = [make_pos(strategy=f"s{i}", symbol="X")
             for i in range(config.MAX_POSITIONS_PER_SYMBOL)]
    res_full = approve(engine, day, positions=crowd, symbol="X", strategy="gap")
    assert isinstance(res_full, Skip) and "X" in res_full.reason


def test_trades_per_day_and_bench():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    day.trades_by_strategy["orb"] = config.MAX_TRADES_PER_DAY_PER_STRATEGY
    assert isinstance(approve(engine, day, strategy="orb"), Skip)

    day2 = DayState(start_equity=100_000)
    for _ in range(config.CONSECUTIVE_LOSSES_TO_BENCH):
        day2.record_trade_result("gap", -100.0)
    assert "gap" in day2.benched_strategies
    assert isinstance(approve(engine, day2, strategy="gap"), Skip)
    # a win resets the streak for others
    day3 = DayState(start_equity=100_000)
    day3.record_trade_result("orb", -1)
    day3.record_trade_result("orb", -1)
    day3.record_trade_result("orb", +50)
    assert "orb" not in day3.benched_strategies


def test_illiquid_and_price_gates():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    st = SymbolState("X", PrevDayLevels(avg_1m_turnover=100_000))  # below min
    assert isinstance(approve(engine, day, sym_state=st), Skip)
    assert isinstance(approve(engine, day, entry=10.0, stop=9.9), Skip)  # penny


def test_circuit_breaker_trips_and_pauses():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    market = MarketState(["X"])
    base = ts(10, 0)
    # feed NIFTY: flat then a 1.5% drop within 15 minutes
    for i in range(16):
        market.on_index_tick("NIFTY", base + timedelta(minutes=i), 25_000)
    market.on_index_tick("NIFTY", base + timedelta(minutes=16), 24_600)
    reason = engine.check_circuit_breaker(market, day, base + timedelta(minutes=16))
    assert reason is not None and "NIFTY" in reason
    assert day.circuit_paused_until is not None
    res = engine.approve(
        strategy="orb", symbol="X", entry_price=500, stop_price=495,
        sym_state=liquid_state(), open_positions=[], equity=100_000,
        margin_used=0, day=day, now=base + timedelta(minutes=17),
    )
    assert isinstance(res, Skip) and "circuit" in res.reason
