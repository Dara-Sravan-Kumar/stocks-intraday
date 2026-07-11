"""Range-day fade (designed for 15m bars): on a classified range day, fade
the edges of the day's range back toward VWAP. The day-type gate — not the
oscillator — is the point; fading trend days is how fades die."""
from __future__ import annotations

from datetime import datetime, timedelta

from bot import daytype
from bot.execution import LONG, SHORT, Position
from bot.state import MarketState, SymbolState
from bot.strategies import ExitRequest, Signal, Strategy


class RangeFade(Strategy):
    name = "range_fade"

    def on_bar_5m(self, st: SymbolState, market: MarketState,
                  now: datetime) -> Signal | None:
        if not self.in_window(now):
            return None
        if self.trades_today(st.symbol) >= self.p["max_trades_per_day"]:
            return None
        if daytype.classify(st, market) != daytype.RANGE:
            return None
        ind = st.ind
        rsi = ind.rsi7.value
        if rsi is None or ind.vwap is None:
            return None
        if ind.day_range_pct is None or ind.day_range_pct < self.p["min_day_range_pct"]:
            return None

        bar = st.bars_5m[-1]
        rng = ind.day_high - ind.day_low
        pos_in_range = (bar.close - ind.day_low) / rng
        buf = self.p["stop_buffer_pct"] / 100.0
        zone = self.p["edge_zone"]

        if pos_in_range <= zone and rsi <= self.p["rsi7_long_below"] \
                and bar.close < ind.vwap:
            reward_pct = (ind.vwap - bar.close) / bar.close * 100.0
            if reward_pct < self.p["min_reward_pct"]:
                return None
            return Signal(self.name, st.symbol, LONG,
                          stop=ind.day_low * (1 - buf), target=ind.vwap,
                          reason=f"range-day fade off low (RSI7 {rsi:.0f})")
        if pos_in_range >= 1 - zone and rsi >= self.p["rsi7_short_above"] \
                and bar.close > ind.vwap:
            reward_pct = (bar.close - ind.vwap) / bar.close * 100.0
            if reward_pct < self.p["min_reward_pct"]:
                return None
            return Signal(self.name, st.symbol, SHORT,
                          stop=ind.day_high * (1 + buf), target=ind.vwap,
                          reason=f"range-day fade off high (RSI7 {rsi:.0f})")
        return None

    def manage(self, pos: Position, st: SymbolState,
               now: datetime) -> ExitRequest | None:
        if now - pos.entry_ts >= timedelta(minutes=self.p["time_stop_min"]):
            return ExitRequest("TIME")
        return None
