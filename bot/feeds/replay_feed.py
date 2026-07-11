"""Replay feed: streams cached bars_1m rows minute-by-minute for backtests
and off-hours dry runs. Drives time — the engine uses bar timestamps as 'now'.
"""
from __future__ import annotations

from datetime import datetime
from itertools import groupby

from bot import db
from bot.bars import Bar
from bot.feeds import Feed


class ReplayFeed(Feed):
    def __init__(self, symbols: list[str], start_ts: str, end_ts: str):
        """start_ts/end_ts: ISO strings compared lexically against bars_1m.ts."""
        self.symbols = symbols
        self.start_ts = start_ts
        self.end_ts = end_ts
        self._minutes: list[list[Bar]] = []
        self._idx = 0

    def start(self) -> None:
        rows = db.load_bars(self.symbols, self.start_ts, self.end_ts)
        bars = [
            Bar(
                symbol=r["symbol"], ts=datetime.fromisoformat(r["ts"]),
                open=r["open"], high=r["high"], low=r["low"], close=r["close"],
                volume=int(r["volume"]),
            )
            for r in rows
        ]
        bars.sort(key=lambda b: (b.ts, b.symbol))
        self._minutes = [list(g) for _, g in groupby(bars, key=lambda b: b.ts)]
        self._idx = 0

    def stop(self) -> None:
        self._minutes = []

    def poll(self) -> list[Bar]:
        if self._idx >= len(self._minutes):
            return []
        batch = self._minutes[self._idx]
        self._idx += 1
        return batch

    @property
    def exhausted(self) -> bool:
        return self._idx >= len(self._minutes)

    @property
    def source_name(self) -> str:
        return "replay"
