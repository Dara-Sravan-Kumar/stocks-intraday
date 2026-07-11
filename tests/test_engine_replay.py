"""End-to-end: scripted synthetic days streamed through the real Engine +
ReplayFeed + PaperBroker, persisted to an in-memory DB."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import config
from bot import db
from bot.clock import IST
from bot.engine import Engine
from bot.execution.paper_broker import PaperBroker
from bot.feeds.replay_feed import ReplayFeed
from bot.risk import RiskEngine
from bot.state import MarketState
from bot.strategies import build_strategies

DAY = "2026-07-06"  # Monday


def ts(h, m):
    return datetime(2026, 7, 6, h, m, tzinfo=IST)


def store(symbol, rows):
    """rows: (ts, o, h, l, c, v)"""
    db.upsert_bars([
        (symbol, t.isoformat(), o, h, l, c, v, "test") for t, o, h, l, c, v in rows
    ])


def orb_breakout_day(symbol="AAA", drift_to=None):
    """OR 500-505 for 09:15-09:29, breakout 09:30-09:34 on volume, then rally
    (or drift if drift_to given). Returns bars until 15:29."""
    rows = []
    for i in range(15):  # opening range
        t = ts(9, 15) + timedelta(minutes=i)
        rows.append((t, 502.0, 505.0 if i == 3 else 503.0,
                     500.0 if i == 7 else 501.0, 502.0, 2000))
    for i in range(5):   # breakout 5m bar on 3x volume
        t = ts(9, 30) + timedelta(minutes=i)
        px = 505.0 + i * 0.5
        rows.append((t, px, px + 0.6, px - 0.4, px + 0.5, 6000))
    # post-breakout path
    minute = 0
    t = ts(9, 35)
    px = 507.5
    end = ts(15, 29)
    while t <= end:
        if drift_to is None:
            px = min(px + 0.35, 520.0)   # rallies through the 2R target (~516.5)
        else:
            px += (drift_to - px) * 0.01  # slow drift, never hits stop/target
        rows.append((t, px, px + 0.3, px - 0.3, px, 2000))
        t += timedelta(minutes=1)
        minute += 1
    return rows


@pytest.fixture()
def engine_env(mem_db):
    def build(symbols, strategies=("orb",)):
        market = MarketState(symbols)
        feed = ReplayFeed(list(symbols), f"{DAY}T00:00", f"{DAY}T23:59")
        broker = PaperBroker(config.PAPER_STARTING_CASH)
        eng = Engine(
            mode="BACKTEST", feed=feed, broker=broker,
            strategies=build_strategies(list(strategies)),
            risk=RiskEngine(), market=market, persist=True,
        )
        return eng, broker
    return build


def test_orb_day_hits_target(engine_env):
    store("AAA", orb_breakout_day("AAA"))
    eng, broker = engine_env(["AAA"])
    eng.run()

    assert eng.n_trades == 1
    trade = eng.closed_trades[0]
    assert trade.position.strategy == "orb"
    assert trade.position.side == "LONG"
    assert trade.exit_reason == "TARGET"
    assert trade.net_pnl > 0
    assert broker.positions == []
    # persisted ledger agrees
    rows = db.trades_for("BACKTEST")
    assert len(rows) == 1
    assert rows[0]["exit_reason"] == "TARGET"
    assert rows[0]["net_pnl"] == pytest.approx(trade.net_pnl)
    # equity derived from ledger
    assert broker.equity({}) == pytest.approx(
        config.PAPER_STARTING_CASH + trade.net_pnl
    )


def test_orb_day_squares_off_when_nothing_hits(engine_env):
    store("BBB", orb_breakout_day("BBB", drift_to=508.0))
    eng, broker = engine_env(["BBB"])
    eng.run()

    assert eng.n_trades == 1
    trade = eng.closed_trades[0]
    assert trade.exit_reason == "SQUAREOFF"
    # squared off at/after 15:12, never later than close
    assert trade.exit_ts.time() >= ts(15, 11).time()
    assert broker.positions == []


def test_no_entries_after_cutoff(engine_env):
    """A breakout that happens after 14:45 must not create a position."""
    rows = []
    for i in range(15):
        t = ts(9, 15) + timedelta(minutes=i)
        rows.append((t, 502.0, 505.0 if i == 3 else 503.0,
                     500.0 if i == 7 else 501.0, 502.0, 2000))
    t = ts(9, 30)
    while t < ts(14, 50):   # flat all day inside the range
        rows.append((t, 503.0, 503.4, 502.6, 503.0, 2000))
        t += timedelta(minutes=1)
    while t <= ts(15, 29):  # late breakout, inside NO_NEW window
        rows.append((t, 507.0, 507.8, 506.5, 507.5, 9000))
        t += timedelta(minutes=1)
    store("CCC", rows)
    eng, _ = engine_env(["CCC"])
    eng.run()
    assert eng.n_trades == 0


def test_daily_loss_halt_closes_and_blocks(engine_env, monkeypatch):
    """Force a huge adverse move after entry; engine must halt for the day."""
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT", 0.05)  # hair trigger
    rows = orb_breakout_day("DDD", drift_to=None)[:25]       # entry path
    # after entry (~09:36), crash far below the stop
    t = ts(9, 40)
    px = 480.0
    while t <= ts(15, 29):
        rows.append((t, px, px + 0.3, px - 0.5, px, 3000))
        px = max(px - 0.5, 460.0)
        t += timedelta(minutes=1)
    store("DDD", rows)
    eng, broker = engine_env(["DDD"])
    eng.run()
    assert eng.day.halted
    assert broker.positions == []
    assert all(t.exit_reason in ("STOP", "HALT") for t in eng.closed_trades)
