from __future__ import annotations

from datetime import datetime

from bot.bars import Bar, Rollup, TickAggregator, floor_minute
from bot.clock import IST


def ts(h, m, s=0):
    return datetime(2026, 7, 6, h, m, s, tzinfo=IST)


def test_floor_minute():
    assert floor_minute(ts(9, 17, 42)) == ts(9, 17)
    assert floor_minute(ts(9, 17), 5) == ts(9, 15)
    assert floor_minute(ts(9, 20), 5) == ts(9, 20)


def test_tick_aggregator_builds_bar_with_volume_deltas():
    agg = TickAggregator("X")
    assert agg.on_tick(ts(9, 15, 1), 100.0, cum_volume=1000) is None
    assert agg.on_tick(ts(9, 15, 20), 101.5, cum_volume=1600) is None
    assert agg.on_tick(ts(9, 15, 50), 99.5, cum_volume=2100) is None
    done = agg.on_tick(ts(9, 16, 2), 100.2, cum_volume=2400)
    assert done is not None
    assert done.ts == ts(9, 15)
    assert (done.open, done.high, done.low, done.close) == (100.0, 101.5, 99.5, 99.5)
    # first tick contributes no delta (no prior cumulative reading)
    assert done.volume == (1600 - 1000) + (2100 - 1600)


def test_tick_aggregator_ignores_out_of_order_ticks():
    agg = TickAggregator("X")
    agg.on_tick(ts(9, 15, 1), 100.0, cum_volume=100)
    agg.on_tick(ts(9, 16, 1), 101.0, cum_volume=200)
    # stale tick for 9:15 after the 9:16 bar opened: dropped
    assert agg.on_tick(ts(9, 15, 59), 90.0, cum_volume=150) is None
    done = agg.flush()
    assert done.ts == ts(9, 16)
    assert done.low == 101.0  # stale 90.0 never applied


def test_tick_aggregator_flush_returns_partial_bar():
    agg = TickAggregator("X")
    agg.on_tick(ts(9, 15, 5), 50.0, cum_volume=10)
    bar = agg.flush()
    assert bar is not None and bar.close == 50.0
    assert agg.flush() is None


def test_rollup_5m_alignment_and_ohlcv():
    r = Rollup("X", 5)
    bars = [
        Bar("X", ts(9, 15), 100, 102, 99, 101, 500),
        Bar("X", ts(9, 16), 101, 103, 100, 102, 300),
        Bar("X", ts(9, 17), 102, 102, 98, 99, 200),
        Bar("X", ts(9, 18), 99, 100, 99, 100, 100),
        Bar("X", ts(9, 19), 100, 101, 100, 101, 400),
    ]
    for b in bars:
        assert r.on_bar(b) is None
    done = r.on_bar(Bar("X", ts(9, 20), 101, 101, 101, 101, 50))
    assert done is not None
    assert done.ts == ts(9, 15)
    assert done.interval == 5
    assert (done.open, done.high, done.low, done.close) == (100, 103, 98, 101)
    assert done.volume == 1500


def test_rollup_handles_gap_in_minutes():
    r = Rollup("X", 5)
    r.on_bar(Bar("X", ts(9, 15), 100, 100, 100, 100, 10))
    # next 1m bar jumps straight to 9:26 (illiquid) -> 9:15 bucket completes
    done = r.on_bar(Bar("X", ts(9, 26), 105, 105, 105, 105, 5))
    assert done is not None and done.ts == ts(9, 15)
    done2 = r.flush()
    assert done2.ts == ts(9, 25)
