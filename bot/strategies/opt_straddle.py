"""Short straddle at the open (run_live --options): sell the ATM CE and PE
just after the opening noise, per-leg premium stop-loss, exit both by a fixed
time. India's most-traded rule-based options strategy — here it must EARN its
way through paper before any real margin ever touches it.

Margin per short lot is approximated from config.OPTIONS["short_margin_per_lot"];
the risk engine blocks legs the paper book can't carry."""
from __future__ import annotations

from datetime import datetime

import config
from bot import clock
from bot.execution import SHORT, Position
from bot.state import MarketState, SymbolState
from bot.strategies import ExitRequest, Signal, Strategy


class OptStraddle(Strategy):
    name = "opt_straddle"
    requires_options = True

    def on_bar_5m(self, st: SymbolState, market: MarketState,
                  now: datetime) -> list[Signal] | None:
        if st.symbol not in config.OPTIONS["underlyings"]:
            return None
        d = now.astimezone(clock.IST).date()
        if not (clock.parse_hhmm(self.p["entry_time"], d) <= now
                <= clock.parse_hhmm(self.p["entry_latest"], d)):
            return None
        if self.trades_today(st.symbol) >= self.p["max_trades_per_day"]:
            return None
        if not st.bars_5m:
            return None
        spot = st.bars_5m[-1].close

        from bot import options as optmod
        chain = market.option_chains.get(st.symbol, [])
        ce = optmod.pick_option(chain, spot, "CE")
        pe = optmod.pick_option(chain, spot, "PE")
        if ce is None or pe is None:
            return None
        legs: list[Signal] = []
        for contract in (ce, pe):
            opt_st = market.get(contract.symbol)
            premium = opt_st.last_price if opt_st else None
            if not premium:
                return None   # both legs or nothing
            stop = premium * (1 + self.p["leg_stop_pct"] / 100.0)
            legs.append(Signal(
                self.name, contract.symbol, SHORT, stop=stop, target=None,
                reason=f"{st.symbol} straddle leg @ {premium:.1f}",
            ))
        self.note_entry(st.symbol, "STRADDLE")
        return legs

    def manage(self, pos: Position, st: SymbolState,
               now: datetime) -> ExitRequest | None:
        exit_dt = clock.parse_hhmm(self.p["exit_time"],
                                   now.astimezone(clock.IST).date())
        if now >= exit_dt:
            return ExitRequest("TIME")
        return None
