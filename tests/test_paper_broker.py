from __future__ import annotations

from datetime import datetime

import pytest

import config
from bot.bars import Bar
from bot.clock import IST
from bot.execution import LONG, SHORT
from bot.execution.paper_broker import (
    PaperBroker, exit_fill_price, stop_hit, target_hit,
)


def ts(h=10, m=0):
    return datetime(2026, 7, 6, h, m, tzinfo=IST)


def test_long_round_trip_pnl_and_costs():
    b = PaperBroker(100_000)
    pos = b.open_position("orb", "X", LONG, 100, 500.0, ts(), stop=495.0, target=510.0)
    slip = 500.0 * config.SLIPPAGE_BPS / 10_000
    assert pos.entry_price == pytest.approx(500.0 + slip)
    assert pos.margin_used == pytest.approx(pos.entry_price * 100 / config.INTRADAY_LEVERAGE)

    trade = b.close_position(pos, 510.0, ts(11), "TARGET")
    exit_slip = 510.0 * config.SLIPPAGE_BPS / 10_000
    assert trade.exit_price == pytest.approx(510.0 - exit_slip)
    expected_gross = (trade.exit_price - pos.entry_price) * 100
    assert trade.gross_pnl == pytest.approx(expected_gross)
    assert trade.costs > 0
    assert trade.net_pnl == pytest.approx(expected_gross - trade.costs)
    assert b.realized_pnl == pytest.approx(trade.net_pnl)
    assert b.positions == []
    assert trade.r_multiple == pytest.approx(
        expected_gross / (abs(pos.entry_price - 495.0) * 100)
    )


def test_short_round_trip():
    b = PaperBroker(100_000)
    pos = b.open_position("gap", "Y", SHORT, 50, 200.0, ts(), stop=204.0, target=192.0)
    assert pos.entry_price < 200.0  # sell fills worse (lower)
    trade = b.close_position(pos, 192.0, ts(11), "TARGET")
    assert trade.exit_price > 192.0  # buy-to-cover fills worse (higher)
    assert trade.gross_pnl == pytest.approx(
        (pos.entry_price - trade.exit_price) * 50
    )
    assert trade.gross_pnl > 0


def test_equity_marks_unrealized():
    b = PaperBroker(100_000)
    b.open_position("orb", "X", LONG, 10, 100.0, ts(), stop=98.0, target=104.0)
    eq = b.equity({"X": 102.0})
    assert eq > 100_000
    eq_down = b.equity({"X": 95.0})
    assert eq_down < 100_000


def test_stop_and_target_hit_detection():
    from bot.execution import Position
    pos = Position("s", "X", LONG, 10, ts(), 100.0, 98.0, 104.0, 200.0, 98.0)
    assert stop_hit(pos, Bar("X", ts(), 99, 99.5, 97.9, 98.5, 0))
    assert not stop_hit(pos, Bar("X", ts(), 99, 99.5, 98.1, 98.5, 0))
    assert target_hit(pos, Bar("X", ts(), 103, 104.2, 102, 104, 0))

    spos = Position("s", "X", SHORT, 10, ts(), 100.0, 102.0, 96.0, 200.0, 102.0)
    assert stop_hit(spos, Bar("X", ts(), 101, 102.5, 100, 101, 0))
    assert target_hit(spos, Bar("X", ts(), 97, 97.5, 95.8, 96.5, 0))


def test_gap_through_stop_fills_at_open():
    from bot.execution import Position
    pos = Position("s", "X", LONG, 10, ts(), 100.0, 98.0, 104.0, 200.0, 98.0)
    # bar opens at 96, well below the 98 stop -> fill at 96, not 98
    bar = Bar("X", ts(10, 5), 96.0, 96.5, 95.0, 96.2, 0)
    assert exit_fill_price(pos, bar, 98.0) == 96.0
    # normal touch: opens above stop, trades through -> fill at stop level
    bar2 = Bar("X", ts(10, 5), 99.0, 99.2, 97.5, 98.2, 0)
    assert exit_fill_price(pos, bar2, 98.0) == 98.0
    # gap UP through target -> fill at the better open
    bar3 = Bar("X", ts(10, 5), 105.0, 105.5, 104.5, 105.2, 0)
    assert exit_fill_price(pos, bar3, 104.0) == 105.0


def test_gap_through_stop_short():
    from bot.execution import Position
    spos = Position("s", "X", SHORT, 10, ts(), 100.0, 102.0, 96.0, 200.0, 102.0)
    # gap up open 103 above stop 102 -> fill at 103 (worse for short)
    bar = Bar("X", ts(10, 5), 103.0, 103.5, 102.5, 103.2, 0)
    assert exit_fill_price(spos, bar, 102.0) == 103.0
    # gap down through target 96 -> fill at open 95 (better)
    bar2 = Bar("X", ts(10, 5), 95.0, 95.5, 94.5, 95.2, 0)
    assert exit_fill_price(spos, bar2, 96.0) == 95.0
