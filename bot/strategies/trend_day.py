"""Trend-day rider (designed for 15m bars): once the day is classified as a
trend day, join it. No fixed target — winners run until the VWAP trail or the
15:12 square-off. The classifier keeps us out on ambiguous days."""
from __future__ import annotations

from datetime import datetime

from bot import daytype
from bot.execution import LONG, SHORT, Position
from bot.state import MarketState, SymbolState
from bot.strategies import ExitRequest, Signal, Strategy


class TrendDay(Strategy):
    name = "trend_day"

    def on_bar_5m(self, st: SymbolState, market: MarketState,
                  now: datetime) -> Signal | None:
        if not self.in_window(now):
            return None
        if self.trades_today(st.symbol) >= self.p["max_trades_per_day"]:
            return None
        ind = st.ind
        rvol = ind.rvol()
        if rvol is not None and rvol < self.p["rvol_min"]:
            return None

        kind = daytype.classify(st, market)
        bar = st.bars_5m[-1]
        buf = self.p["stop_vwap_buffer_pct"] / 100.0

        if kind == daytype.TREND_UP:
            stop = max(ind.day_low, ind.vwap * (1 - buf))
            risk = bar.close - stop
            if risk <= 0 or risk / bar.close * 100.0 > self.p["max_risk_pct"]:
                return None
            return Signal(self.name, st.symbol, LONG, stop=stop, target=None,
                          reason=f"trend day up ({ind.day_change_pct:+.1f}%), ride to close")
        if kind == daytype.TREND_DOWN:
            stop = min(ind.day_high, ind.vwap * (1 + buf))
            risk = stop - bar.close
            if risk <= 0 or risk / bar.close * 100.0 > self.p["max_risk_pct"]:
                return None
            return Signal(self.name, st.symbol, SHORT, stop=stop, target=None,
                          reason=f"trend day down ({ind.day_change_pct:+.1f}%), ride to close")
        return None

    def manage(self, pos: Position, st: SymbolState,
               now: datetime) -> ExitRequest | None:
        """After +trail_after_r, trail the stop along VWAP — the trend-day line."""
        price = st.last_price
        vwap = st.ind.vwap
        if price is None or vwap is None:
            return None
        r = pos.risk_per_share * self.p["trail_after_r"]
        if pos.is_long and price >= pos.entry_price + r:
            pos.stop_price = max(pos.stop_price, vwap)
        elif not pos.is_long and price <= pos.entry_price - r:
            pos.stop_price = min(pos.stop_price, vwap)
        return None
