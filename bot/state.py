"""In-memory market state shared with strategies. No I/O here.

SymbolState is everything a strategy may see about one symbol; MarketState
adds index (NIFTY/BANKNIFTY) tracking for the circuit breaker.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import config
from bot.bars import Bar, Rollup
from bot.indicators import Indicators, PrevDayLevels


class SymbolState:
    def __init__(self, symbol: str, prev_day: PrevDayLevels | None = None,
                 option_meta=None):
        self.symbol = symbol
        self.bars_1m: deque[Bar] = deque(maxlen=500)
        self.bars_5m: list[Bar] = []
        self.ind = Indicators(symbol, prev_day)
        self._rollup = Rollup(symbol)
        self.option_meta = option_meta          # OptionContract | None
        self.lot_size: int = option_meta.lot if option_meta else 1

    def on_bar_1m(self, bar: Bar) -> Bar | None:
        """Feed a completed 1m bar; returns a completed 5m bar if one closed."""
        self.bars_1m.append(bar)
        self.ind.on_bar_1m(bar)
        done_5m = self._rollup.on_bar(bar)
        if done_5m is not None:
            self.bars_5m.append(done_5m)
            self.ind.on_bar_5m(done_5m)
        return done_5m

    def flush_5m(self) -> Bar | None:
        done = self._rollup.flush()
        if done is not None:
            self.bars_5m.append(done)
            self.ind.on_bar_5m(done)
        return done

    @property
    def last_price(self) -> float | None:
        return self.bars_1m[-1].close if self.bars_1m else None

    @property
    def last_ts(self) -> datetime | None:
        return self.bars_1m[-1].ts if self.bars_1m else None

    def avg_1m_turnover(self) -> float | None:
        """Session-observed fallback when history didn't provide one."""
        if self.ind.prev_day.avg_1m_turnover:
            return self.ind.prev_day.avg_1m_turnover
        if len(self.bars_1m) < 5:
            return None
        recent = list(self.bars_1m)[-30:]
        return sum(b.close * b.volume for b in recent) / len(recent)


@dataclass
class IndexTracker:
    """Rolling index closes for the circuit breaker."""
    symbol: str
    day_open: float | None = None
    last: float | None = None
    _window: deque = field(default_factory=lambda: deque(maxlen=60))  # (ts, price)

    def update(self, ts: datetime, price: float) -> None:
        if self.day_open is None:
            self.day_open = price
        self.last = price
        self._window.append((ts, price))

    def move_pct_last(self, minutes: int, now: datetime) -> float | None:
        cutoff = now - timedelta(minutes=minutes)
        past = [p for t, p in self._window if t <= cutoff]
        ref = past[-1] if past else (self._window[0][1] if len(self._window) > 1 else None)
        if ref is None or self.last is None or ref == 0:
            return None
        return (self.last - ref) / ref * 100.0

    def move_pct_from_open(self) -> float | None:
        if self.day_open in (None, 0) or self.last is None:
            return None
        return (self.last - self.day_open) / self.day_open * 100.0


class MarketState:
    def __init__(self, symbols: list[str],
                 prev_day: dict[str, PrevDayLevels] | None = None,
                 option_contracts: dict | None = None):
        prev_day = prev_day or {}
        option_contracts = option_contracts or {}
        self.symbols: dict[str, SymbolState] = {
            s: SymbolState(s, prev_day.get(s), option_contracts.get(s))
            for s in symbols
        }
        self.indices: dict[str, IndexTracker] = {
            name: IndexTracker(name) for name in config.INDEX_SYMBOLS
        }
        # underlying -> list[OptionContract] for strategies picking strikes
        self.option_chains: dict[str, list] = {}
        for c in option_contracts.values():
            self.option_chains.setdefault(c.underlying, []).append(c)
        self.session_date: str | None = None

    def get(self, symbol: str) -> SymbolState | None:
        return self.symbols.get(symbol)

    def on_index_tick(self, name: str, ts: datetime, price: float) -> None:
        if name in self.indices:
            self.indices[name].update(ts, price)
