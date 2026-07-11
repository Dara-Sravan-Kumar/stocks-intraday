"""Momentum breakout: 5m close beyond the previous day's high/low with strong
relative volume, avoiding names already overextended on the day."""
from __future__ import annotations

from datetime import datetime

from bot.execution import LONG, SHORT, Position
from bot.state import MarketState, SymbolState
from bot.strategies import ExitRequest, Signal, Strategy


class MomentumBreakout(Strategy):
    name = "momentum_breakout"

    def on_bar_5m(self, st: SymbolState, market: MarketState,
                  now: datetime) -> Signal | None:
        if not self.in_window(now):
            return None
        ind = st.ind
        pd = ind.prev_day
        if pd.high is None or pd.low is None:
            return None
        rvol = ind.rvol()
        if rvol is None or rvol < self.p["rvol_min"]:
            return None
        if pd.avg_daily_range_pct and ind.day_range_pct is not None and \
                ind.day_range_pct > self.p["max_range_vs_avg"] * pd.avg_daily_range_pct:
            return None  # already extended

        bar = st.bars_5m[-1]
        floor_dist = bar.close * self.p["stop_max_pct"] / 100.0

        if bar.close > pd.high and \
                self.trades_today(st.symbol, LONG) < self.p["max_trades_per_direction"]:
            stop = min(bar.low, bar.close - floor_dist)
            risk = bar.close - stop
            if risk / bar.close * 100.0 > self.p["max_risk_pct"]:
                return None
            return Signal(self.name, st.symbol, LONG, stop=stop,
                          target=bar.close + self.p["target_r"] * risk,
                          reason=f"PDH breakout, RVOL {rvol:.1f}")
        if bar.close < pd.low and \
                self.trades_today(st.symbol, SHORT) < self.p["max_trades_per_direction"]:
            stop = max(bar.high, bar.close + floor_dist)
            risk = stop - bar.close
            if risk / bar.close * 100.0 > self.p["max_risk_pct"]:
                return None
            return Signal(self.name, st.symbol, SHORT, stop=stop,
                          target=bar.close - self.p["target_r"] * risk,
                          reason=f"PDL breakdown, RVOL {rvol:.1f}")
        return None

    def manage(self, pos: Position, st: SymbolState,
               now: datetime) -> ExitRequest | None:
        # After +trail_after_r, trail the stop along EMA20(5m).
        price = st.last_price
        ema = st.ind.ema20.value
        if price is None or ema is None:
            return None
        r = pos.risk_per_share * self.p["trail_after_r"]
        if pos.is_long and price >= pos.entry_price + r:
            pos.stop_price = max(pos.stop_price, ema)
        elif not pos.is_long and price <= pos.entry_price - r:
            pos.stop_price = min(pos.stop_price, ema)
        return None
