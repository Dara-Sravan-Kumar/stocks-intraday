"""Bar primitives and tick->1m->5m aggregation.

Feeds emit only COMPLETED bars: a 1m bar for minute M is emitted once a tick
(or bar) for a later minute arrives, or on explicit flush at session end.
Dhan Quote packets carry the day's cumulative volume, so per-bar volume is the
delta between cumulative readings.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import config


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime          # aware IST, start of the bar's interval
    open: float
    high: float
    low: float
    close: float
    volume: int
    interval: int = 1     # minutes

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0


def floor_minute(ts: datetime, interval: int = 1) -> datetime:
    ts = ts.replace(second=0, microsecond=0)
    return ts - timedelta(minutes=ts.minute % interval)


class TickAggregator:
    """Per-symbol tick -> 1m bar builder using cumulative day volume deltas."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self._cur: Bar | None = None
        self._last_cum_vol: int | None = None

    def on_tick(self, ts: datetime, ltp: float, cum_volume: int | None = None) -> Bar | None:
        """Returns the completed previous 1m bar when a new minute starts."""
        minute = floor_minute(ts)
        vol_delta = 0
        if cum_volume is not None:
            if self._last_cum_vol is not None and cum_volume >= self._last_cum_vol:
                vol_delta = cum_volume - self._last_cum_vol
            self._last_cum_vol = cum_volume

        completed: Bar | None = None
        cur = self._cur
        if cur is None or minute > cur.ts:
            completed = cur
            self._cur = Bar(self.symbol, minute, ltp, ltp, ltp, ltp, vol_delta)
        elif minute == cur.ts:
            self._cur = replace(
                cur,
                high=max(cur.high, ltp),
                low=min(cur.low, ltp),
                close=ltp,
                volume=cur.volume + vol_delta,
            )
        # ticks for an older minute (out of order) are dropped
        return completed

    def flush(self) -> Bar | None:
        cur, self._cur = self._cur, None
        return cur


class Rollup:
    """Per-symbol 1m -> Nm rollup. Emits the completed Nm bar when a 1m bar
    belonging to the next bucket arrives."""

    def __init__(self, symbol: str, interval: int | None = None):
        self.symbol = symbol
        self.interval = interval or config.STRATEGY_INTERVAL_MIN
        self._cur: Bar | None = None

    def on_bar(self, bar: Bar) -> Bar | None:
        bucket = floor_minute(bar.ts, self.interval)
        completed: Bar | None = None
        cur = self._cur
        if cur is None or bucket > cur.ts:
            completed = cur
            self._cur = Bar(self.symbol, bucket, bar.open, bar.high, bar.low,
                            bar.close, bar.volume, self.interval)
        elif bucket == cur.ts:
            self._cur = replace(
                cur,
                high=max(cur.high, bar.high),
                low=min(cur.low, bar.low),
                close=bar.close,
                volume=cur.volume + bar.volume,
            )
        return completed

    def flush(self) -> Bar | None:
        cur, self._cur = self._cur, None
        return cur
