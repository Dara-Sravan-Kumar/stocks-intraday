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


def test_sizing_formula():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    res = approve(engine, day, entry=500.0, stop=490.0)
    assert isinstance(res, Approval)
    # risk 1% of 1L = 1000; risk/share = 10 -> 100 shares
    # notional 100*500 = 50,000 <= 60% cap -> stands
    assert res.qty == 100
    assert res.margin == pytest.approx(100 * 500 / config.INTRADAY_LEVERAGE)


def test_notional_cap_reduces_qty():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    # tight-ish stop -> huge qty by risk; capped by 60% notional
    res = approve(engine, day, entry=500.0, stop=498.0)
    assert isinstance(res, Approval)
    assert res.qty == math.floor(60_000 / 500)


def test_stop_distance_gate_blocks_fee_eaten_setups():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    res = approve(engine, day, entry=500.0, stop=499.8)  # 0.04% stop distance
    assert isinstance(res, Skip) and "too tight" in res.reason


def test_margin_cap_reduces_qty():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    res = approve(engine, day, entry=500.0, stop=498.0, margin_used=85_000)
    # margin cap 90k; only 5k free -> 25k notional -> 50 shares
    assert isinstance(res, Approval)
    assert res.qty == 50


def test_daily_halt_blocks():
    engine = RiskEngine()
    day = DayState(start_equity=100_000, halted=True, halt_reason="max daily loss")
    res = approve(engine, day)
    assert isinstance(res, Skip) and "halted" in res.reason


def test_daily_loss_breach_detection():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    assert not engine.daily_loss_breached(98_100, day)
    assert engine.daily_loss_breached(98_000, day)   # exactly -2%


def test_max_concurrent_positions():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    positions = [make_pos(symbol=f"P{i}") for i in range(config.MAX_CONCURRENT_POSITIONS)]
    res = approve(engine, day, positions=positions)
    assert isinstance(res, Skip) and "concurrent" in res.reason


def test_per_strategy_and_per_symbol_limits():
    engine = RiskEngine()
    day = DayState(start_equity=100_000)
    per_strat = [make_pos(strategy="orb", symbol=f"P{i}")
                 for i in range(config.MAX_POSITIONS_PER_STRATEGY)]
    res = approve(engine, day, positions=per_strat, strategy="orb")
    assert isinstance(res, Skip) and "orb" in res.reason

    res2 = approve(engine, day, positions=[make_pos(symbol="X")], symbol="X",
                   strategy="gap")
    assert isinstance(res2, Skip) and "X" in res2.reason


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
