"""VWAP mean reversion: fade closes stretched beyond VWAP ± k·σ on non-trend
days, targeting a return to VWAP."""
from __future__ import annotations

from datetime import datetime, timedelta

from bot.execution import LONG, SHORT, Position
from bot.state import MarketState, SymbolState
from bot.strategies import ExitRequest, Signal, Strategy


class VwapReversion(Strategy):
    name = "vwap_reversion"

    def on_bar_5m(self, st: SymbolState, market: MarketState,
                  now: datetime) -> Signal | None:
        if not self.in_window(now):
            return None
        ind = st.ind
        band = ind.vwap_band(self.p["band_sigma"])
        rsi = ind.rsi14.value
        if band is None or rsi is None or ind.day_change_pct is None:
            return None
        if abs(ind.day_change_pct) > self.p["max_day_change_pct"]:
            return None
        if self.trades_today(st.symbol) >= self.p["max_trades_per_day"]:
            return None

        lower, upper = band
        bar = st.bars_5m[-1]
        stop_dist = max(self.p["stop_sigma"] * ind.vwap_sigma,
                        bar.close * self.p["stop_floor_pct"] / 100.0)

        if bar.close > upper and rsi > self.p["rsi_overbought"]:
            return Signal(self.name, st.symbol, SHORT,
                          stop=bar.close + stop_dist, target=ind.vwap,
                          reason=f"fade +{self.p['band_sigma']}σ, RSI {rsi:.0f}")
        if bar.close < lower and rsi < self.p["rsi_oversold"]:
            return Signal(self.name, st.symbol, LONG,
                          stop=bar.close - stop_dist, target=ind.vwap,
                          reason=f"fade -{self.p['band_sigma']}σ, RSI {rsi:.0f}")
        return None

    def manage(self, pos: Position, st: SymbolState,
               now: datetime) -> ExitRequest | None:
        if now - pos.entry_ts >= timedelta(minutes=self.p["time_stop_min"]):
            return ExitRequest("TIME")
        return None
