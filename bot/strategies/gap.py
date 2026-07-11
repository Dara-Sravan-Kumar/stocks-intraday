"""Opening gap plays. Large gaps that hold: trade continuation past the first
5m bar's extreme (gap-and-go). Small gaps with a weak first bar: fade toward
the previous close (gap fill)."""
from __future__ import annotations

from datetime import datetime

from bot.execution import LONG, SHORT
from bot.state import MarketState, SymbolState
from bot.strategies import Signal, Strategy


class Gap(Strategy):
    name = "gap"

    def on_bar_5m(self, st: SymbolState, market: MarketState,
                  now: datetime) -> Signal | None:
        if not self.in_window(now):
            return None
        ind = st.ind
        gap = ind.gap_pct
        prev_close = ind.prev_day.close
        if gap is None or prev_close is None or not st.bars_5m:
            return None
        if self.trades_today(st.symbol) >= self.p["max_trades_per_day"]:
            return None

        first = st.bars_5m[0]
        bar = st.bars_5m[-1]
        if bar.ts == first.ts:
            return None  # need the first bar completed AND a later trigger bar
        gap_abs = abs(gap)
        gap_points = ind.day_open - prev_close

        # --- gap-and-go: large gap, first bar agrees, price takes out its extreme
        if self.p["go_gap_min_pct"] <= gap_abs <= self.p["go_gap_max_pct"]:
            hold = self.p["go_hold_frac"]
            if gap > 0 and first.close > first.open and \
                    bar.close > first.high and \
                    bar.close >= prev_close + hold * gap_points:
                risk = bar.close - first.low
                if risk <= 0:
                    return None
                return Signal(self.name, st.symbol, LONG, stop=first.low,
                              target=bar.close + self.p["target_r"] * risk,
                              reason=f"gap-and-go long, gap {gap:+.2f}%")
            if gap < 0 and first.close < first.open and \
                    bar.close < first.low and \
                    bar.close <= prev_close + hold * gap_points:
                risk = first.high - bar.close
                if risk <= 0:
                    return None
                return Signal(self.name, st.symbol, SHORT, stop=first.high,
                              target=bar.close - self.p["target_r"] * risk,
                              reason=f"gap-and-go short, gap {gap:+.2f}%")

        # --- gap-fade: modest gap, weak first bar, play the fill to prev close
        if self.p["fade_gap_min_pct"] <= gap_abs < self.p["fade_gap_max_pct"]:
            buf = self.p["fade_stop_buffer_pct"] / 100.0
            if gap > 0 and first.close < first.open and bar.close < first.low:
                stop = (ind.day_high or first.high) * (1 + buf)
                if stop <= bar.close or prev_close >= bar.close:
                    return None
                return Signal(self.name, st.symbol, SHORT, stop=stop,
                              target=prev_close,
                              reason=f"gap-fade short toward {prev_close:.2f}")
            if gap < 0 and first.close > first.open and bar.close > first.high:
                stop = (ind.day_low or first.low) * (1 - buf)
                if stop >= bar.close or prev_close <= bar.close:
                    return None
                return Signal(self.name, st.symbol, LONG, stop=stop,
                              target=prev_close,
                              reason=f"gap-fade long toward {prev_close:.2f}")
        return None
