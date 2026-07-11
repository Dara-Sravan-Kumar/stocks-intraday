"""Free fallback feed: one batched yfinance 1m download per minute.

Effective lag is ~1-2 minutes. Only fully-elapsed minutes are emitted; the
in-progress bar is held back. Emitted bars are also cached into bars_1m so
mid-session restarts and future backtests have the data.
"""
from __future__ import annotations

import logging
import time as time_mod
from datetime import datetime, timedelta

import config
from bot import clock, db
from bot.bars import Bar
from bot.feeds import Feed

log = logging.getLogger(__name__)


class YfFeed(Feed):
    def __init__(self, symbols: list[str]):
        self.symbols = symbols                     # NSE symbols (no suffix)
        self._emitted: set[tuple[str, str]] = set()
        self._last_fetch = 0.0
        self._all = symbols + list(config.INDEX_SYMBOLS)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    @property
    def source_name(self) -> str:
        return "yfinance"

    def _yf_symbol(self, symbol: str) -> str:
        return config.INDEX_SYMBOLS.get(symbol, f"{symbol}{config.YF_SUFFIX}")

    def poll(self) -> list[Bar]:
        now = time_mod.monotonic()
        if now - self._last_fetch < config.YF_POLL_SECONDS:
            return []
        self._last_fetch = now
        try:
            return self._fetch()
        except Exception as exc:  # noqa: BLE001 — network flake: try next minute
            log.warning("yf feed poll failed: %s", exc)
            return []

    def _fetch(self) -> list[Bar]:
        import pandas as pd
        import yfinance as yf

        tickers = [self._yf_symbol(s) for s in self._all]
        df = yf.download(tickers=tickers, period="1d", interval="1m",
                         group_by="ticker", auto_adjust=False,
                         threads=True, progress=False)
        if df is None or df.empty:
            return []

        cutoff = clock.now_ist().replace(second=0, microsecond=0) - timedelta(minutes=1)
        out: list[Bar] = []
        cache_rows: list[tuple] = []
        multi = isinstance(df.columns, pd.MultiIndex)
        for sym in self._all:
            ysym = self._yf_symbol(sym)
            try:
                sub = df[ysym] if multi else df
            except KeyError:
                continue
            sub = sub.dropna(subset=["Open", "High", "Low", "Close"])
            if sub.empty:
                continue
            idx = sub.index
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            idx = idx.tz_convert(clock.IST)
            for ts, r in zip(idx, sub.itertuples(index=False)):
                ts_py = ts.to_pydatetime()
                if ts_py > cutoff:
                    continue  # bar may still be forming
                key = (sym, ts_py.isoformat())
                if key in self._emitted:
                    continue
                self._emitted.add(key)
                vol = int(r.Volume) if r.Volume == r.Volume else 0
                out.append(Bar(sym, ts_py, float(r.Open), float(r.High),
                               float(r.Low), float(r.Close), vol))
                cache_rows.append((sym, ts_py.isoformat(), float(r.Open),
                                   float(r.High), float(r.Low),
                                   float(r.Close), vol, "yf"))
        if cache_rows:
            try:
                db.upsert_bars(cache_rows)
            except Exception as exc:  # noqa: BLE001
                log.warning("bar cache write failed: %s", exc)
        out.sort(key=lambda b: (b.ts, b.symbol))
        if out:
            log.debug("yf feed emitted %d bars up to %s", len(out), out[-1].ts)
        return out
