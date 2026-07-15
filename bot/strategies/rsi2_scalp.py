"""RSI(2) with-trend scalp: buy deep short-term oversold above VWAP (mirror
short below VWAP). Quick profit target, tight stop, hard time stop."""
from __future__ import annotations

from datetime import datetime, timedelta

from bot.execution import LONG, SHORT, Position
from bot.state import MarketState, SymbolState
from bot.strategies import ExitRequest, Signal, Strategy


class Rsi2Scalp(Strategy):
    name = "rsi2_scalp"

    def on_bar_5m(self, st: SymbolState, market: MarketState,
                  now: datetime) -> Signal | None:
        if not self.in_window(now):
            return None
        ind = st.ind
        rsi = ind.rsi2.value
        if rsi is None or ind.vwap is None:
            return None
        if self.trades_today(st.symbol) >= self.p["max_trades_per_day"]:
            return None

        bar = st.bars_5m[-1]
        stop_frac = self.p["stop_pct"] / 100.0
        tp_frac = self.p["take_profit_pct"] / 100.0

        if rsi < self.p["long_below"] and bar.close > ind.vwap:
            return Signal(self.name, st.symbol, LONG,
                          stop=bar.close * (1 - stop_frac),
                          target=bar.close * (1 + tp_frac),
                          reason=f"RSI2 {rsi:.0f} above VWAP")
        if rsi > self.p["short_above"] and bar.close < ind.vwap:
            return Signal(self.name, st.symbol, SHORT,
                          stop=bar.close * (1 + stop_frac),
                          target=bar.close * (1 - tp_frac),
                          reason=f"RSI2 {rsi:.0f} below VWAP")
        return None

    def manage(self, pos: Position, st: SymbolState,
               now: datetime) -> ExitRequest | None:
        if now - pos.entry_ts >= timedelta(minutes=self.p["time_stop_min"]):
            return ExitRequest("TIME")
        rsi = st.ind.rsi2.value
        if rsi is not None:
            # Absolute-state exit: RSI leaving oversold/overbought can already be
            # true on the entry bar, so it's SOFT — grace-gated by the engine.
            if pos.is_long and rsi > self.p["exit_rsi_long"]:
                return ExitRequest("RSI_EXIT", soft=True)
            if not pos.is_long and rsi < self.p["exit_rsi_short"]:
                return ExitRequest("RSI_EXIT", soft=True)
        return None
