"""Task 3a — the soft-exit grace period.

A SOFT ("setup broken") exit that reads absolute instrument state is suppressed
until the position has been held MIN_HOLD_BARS_BEFORE_SOFT_EXIT bars; hard
stop/target hits and the end-of-day square-off still fire on any bar.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import config
from bot.bars import Bar
from bot.clock import IST
from bot.engine import Engine
from bot.execution import LONG
from bot.execution.paper_broker import PaperBroker
from bot.risk import DayState, RiskEngine
from bot.state import MarketState
from bot.strategies import ExitRequest, Signal, Strategy

BASE = datetime(2026, 7, 6, 10, 0, tzinfo=IST)


class _SoftExit(Strategy):
    name = "soft"

    def on_bar_5m(self, st, market, now):  # pragma: no cover - unused here
        return None

    def manage(self, pos, st, now):
        return ExitRequest("SETUP_BROKEN", soft=True)


class _StubFeed:
    def start(self): pass
    def stop(self): pass
    def poll(self): return []
    @property
    def exhausted(self): return False
    @property
    def source_name(self): return "replay"


def _engine():
    strat = _SoftExit(params={})
    eng = Engine(mode="PAPER", feed=_StubFeed(), broker=PaperBroker(100_000.0),
                 strategies=[strat], risk=RiskEngine(),
                 market=MarketState(["TEST"], {}), persist=False,
                 require_fyers_feed=False)
    eng.day = DayState(start_equity=100_000.0)
    return eng


def _open(eng):
    return eng.broker.open_position("soft", "TEST", LONG, 10, ref_price=100.0,
                                    ts=BASE, stop=99.0, target=102.0, margin=200.0)


def _bar(minute: int, close: float = 100.5) -> Bar:
    ts = BASE + timedelta(minutes=minute)
    return Bar(symbol="TEST", ts=ts, open=close, high=max(close, 100.5),
               low=min(close, 100.5), close=close, volume=1000, interval=1)


def test_soft_exit_suppressed_within_grace_then_fires():
    assert config.MIN_HOLD_BARS_BEFORE_SOFT_EXIT == 2
    eng = _engine()
    pos = _open(eng)

    # +5 min -> 1 bar held (< 2): soft exit suppressed, position stays open.
    eng.now = BASE + timedelta(minutes=5)
    eng._manage_positions(_bar(4))
    assert pos in eng.broker.positions

    # +15 min -> 3 bars held (>= 2): soft exit now fires.
    eng.now = BASE + timedelta(minutes=15)
    eng._manage_positions(_bar(14))
    assert pos not in eng.broker.positions
    assert eng.closed_trades[-1].exit_reason == "SETUP_BROKEN"


def test_hard_stop_fires_within_grace():
    eng = _engine()
    pos = _open(eng)
    eng.now = BASE + timedelta(minutes=2)   # 0 bars held (< grace)
    # a bar that trades through the stop, timestamped after entry
    bar = Bar(symbol="TEST", ts=BASE + timedelta(minutes=1), open=99.5, high=99.6,
              low=98.5, close=98.7, volume=1000, interval=1)
    eng._check_price_exits(bar)
    assert pos not in eng.broker.positions
    assert eng.closed_trades[-1].exit_reason == "STOP"


def test_square_off_fires_within_grace():
    eng = _engine()
    pos = _open(eng)
    eng.now = BASE + timedelta(minutes=2)   # 0 bars held (< grace)
    eng._square_off_all("SQUAREOFF")
    assert pos not in eng.broker.positions
    assert eng.closed_trades[-1].exit_reason == "SQUAREOFF"
