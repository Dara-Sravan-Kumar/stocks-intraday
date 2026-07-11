"""Opening Range Breakout: first 15 minutes define the range; trade the first
5m close beyond it with volume confirmation."""
from __future__ import annotations

from datetime import datetime

from bot import clock
from bot.execution import LONG, SHORT
from bot.state import MarketState, SymbolState
from bot.strategies import Signal, Strategy


class Orb(Strategy):
    name = "orb"

    def on_bar_5m(self, st: SymbolState, market: MarketState,
                  now: datetime) -> Signal | None:
        ind = st.ind
        if not ind.or_done or ind.or_high is None or ind.or_low is None:
            return None
        deadline = clock.parse_hhmm(self.p["entry_deadline"],
                                    now.astimezone(clock.IST).date())
        if now >= deadline:
            return None

        bar = st.bars_5m[-1]
        or_range = ind.or_high - ind.or_low
        or_range_pct = or_range / bar.close * 100.0
        if not (self.p["min_or_range_pct"] <= or_range_pct <= self.p["max_or_range_pct"]):
            return None

        n_or_bars = max(1, self.p["or_minutes"] // 5)
        mean_or_vol = ind.or_volume / n_or_bars
        if mean_or_vol <= 0 or bar.volume < self.p["breakout_vol_mult"] * mean_or_vol:
            return None

        mid = (ind.or_high + ind.or_low) / 2.0
        if bar.close > ind.or_high and \
                self.trades_today(st.symbol, LONG) < self.p["max_trades_per_direction"]:
            risk = bar.close - mid
            return Signal(self.name, st.symbol, LONG, stop=mid,
                          target=bar.close + self.p["target_r"] * risk,
                          reason=f"ORB long: close {bar.close:.2f} > OR high {ind.or_high:.2f}")
        if bar.close < ind.or_low and \
                self.trades_today(st.symbol, SHORT) < self.p["max_trades_per_direction"]:
            risk = mid - bar.close
            return Signal(self.name, st.symbol, SHORT, stop=mid,
                          target=bar.close - self.p["target_r"] * risk,
                          reason=f"ORB short: close {bar.close:.2f} < OR low {ind.or_low:.2f}")
        return None
