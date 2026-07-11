"""Trend-day option buyer (run_live --options): when the index is having an
unmistakable directional day (big move, closing near the extreme, right side
of EMA20), buy the ATM option and hold toward the close with a premium stop
and a hard exit time. Indices have no volume, so this uses price structure
only — no VWAP/RVOL."""
from __future__ import annotations

from datetime import datetime

import config
from bot import clock
from bot.execution import LONG, Position
from bot.state import MarketState, SymbolState
from bot.strategies import ExitRequest, Signal, Strategy


class OptTrendDay(Strategy):
    name = "opt_trend_day"
    requires_options = True

    def on_bar_5m(self, st: SymbolState, market: MarketState,
                  now: datetime) -> Signal | None:
        if st.symbol not in config.OPTIONS["underlyings"]:
            return None
        if not self.in_window(now):
            return None
        if self.trades_today(st.symbol) >= self.p["max_trades_per_day"]:
            return None
        ind = st.ind
        if ind.day_change_pct is None or ind.day_high is None:
            return None
        rng = ind.day_high - ind.day_low
        if rng <= 0:
            return None
        ema = ind.ema20.value
        bar = st.bars_5m[-1]
        pos_in_range = (bar.close - ind.day_low) / rng

        opt_type = None
        if (ind.day_change_pct >= self.p["min_day_change_pct"]
                and pos_in_range >= self.p["range_pos"]
                and (ema is None or bar.close > ema)):
            opt_type = "CE"
        elif (ind.day_change_pct <= -self.p["min_day_change_pct"]
                and pos_in_range <= 1 - self.p["range_pos"]
                and (ema is None or bar.close < ema)):
            opt_type = "PE"
        if opt_type is None:
            return None

        from bot import options as optmod
        chain = market.option_chains.get(st.symbol, [])
        contract = optmod.pick_option(chain, bar.close, opt_type)
        if contract is None:
            return None
        opt_st = market.get(contract.symbol)
        premium = opt_st.last_price if opt_st else None
        if not premium:
            return None
        self.note_entry(st.symbol, opt_type)
        stop = premium * (1 - self.p["premium_stop_pct"] / 100.0)
        return Signal(self.name, contract.symbol, LONG, stop=stop, target=None,
                      reason=f"{st.symbol} trend day {ind.day_change_pct:+.1f}% "
                             f"→ buy {contract.symbol.split(':')[1]}")

    def manage(self, pos: Position, st: SymbolState,
               now: datetime) -> ExitRequest | None:
        exit_dt = clock.parse_hhmm(self.p["exit_time"],
                                   now.astimezone(clock.IST).date())
        if now >= exit_dt:
            return ExitRequest("TIME")
        return None
