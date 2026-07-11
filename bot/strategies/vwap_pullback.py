"""Trend-day VWAP pullback: on an established trend day, enter when price
pulls back to VWAP, holds, and closes back on the trend side."""
from __future__ import annotations

from datetime import datetime

from bot.execution import LONG, SHORT, Position
from bot.state import MarketState, SymbolState
from bot.strategies import ExitRequest, Signal, Strategy


class VwapPullback(Strategy):
    name = "vwap_pullback"

    def on_bar_5m(self, st: SymbolState, market: MarketState,
                  now: datetime) -> Signal | None:
        if not self.in_window(now):
            return None
        ind = st.ind
        if ind.vwap is None or ind.day_change_pct is None:
            return None
        if self.trades_today(st.symbol) >= self.p["max_trades_per_day"]:
            return None
        slope_up = ind.vwap_slope_up(self.p["vwap_slope_bars"])
        if slope_up is None:
            return None

        bar = st.bars_5m[-1]
        tol = self.p["touch_tolerance_pct"] / 100.0
        buf = self.p["stop_buffer_pct"] / 100.0

        # Long: uptrend day, pullback tags VWAP, closes back above.
        if (ind.minutes_above_vwap >= self.p["min_side_minutes"] and slope_up
                and ind.day_change_pct >= self.p["min_day_change_pct"]
                and bar.low <= ind.vwap * (1 + tol) and bar.close > ind.vwap):
            stop = bar.low * (1 - buf)
            risk = bar.close - stop
            if risk <= 0 or risk / bar.close * 100.0 > self.p["max_risk_pct"]:
                return None
            return Signal(self.name, st.symbol, LONG, stop=stop,
                          target=bar.close + self.p["target_r"] * risk,
                          reason=f"VWAP pullback long @ {ind.vwap:.2f}")

        # Short mirror: downtrend day.
        if (ind.minutes_below_vwap >= self.p["min_side_minutes"] and slope_up is False
                and ind.day_change_pct <= -self.p["min_day_change_pct"]
                and bar.high >= ind.vwap * (1 - tol) and bar.close < ind.vwap):
            stop = bar.high * (1 + buf)
            risk = stop - bar.close
            if risk <= 0 or risk / bar.close * 100.0 > self.p["max_risk_pct"]:
                return None
            return Signal(self.name, st.symbol, SHORT, stop=stop,
                          target=bar.close - self.p["target_r"] * risk,
                          reason=f"VWAP pullback short @ {ind.vwap:.2f}")
        return None

    def manage(self, pos: Position, st: SymbolState,
               now: datetime) -> ExitRequest | None:
        # Move stop to breakeven once up breakeven_at_r multiples.
        price = st.last_price
        if price is None or pos.scratch.get("breakeven_done"):
            return None
        r = pos.risk_per_share * self.p["breakeven_at_r"]
        if pos.is_long and price >= pos.entry_price + r:
            pos.stop_price = max(pos.stop_price, pos.entry_price)
            pos.scratch["breakeven_done"] = True
        elif not pos.is_long and price <= pos.entry_price - r:
            pos.stop_price = min(pos.stop_price, pos.entry_price)
            pos.scratch["breakeven_done"] = True
        return None
