"""Index ORB with options (run_live --options): when NIFTY/BANKNIFTY breaks
its opening range, BUY the ATM call (upside) or put (downside). Defined risk =
premium stop; index signals, option instruments."""
from __future__ import annotations

from datetime import datetime

import config
from bot import clock
from bot.execution import LONG, Position
from bot.state import MarketState, SymbolState
from bot.strategies import ExitRequest, Signal, Strategy


class OptOrb(Strategy):
    name = "opt_orb"
    requires_options = True

    def on_bar_5m(self, st: SymbolState, market: MarketState,
                  now: datetime) -> Signal | None:
        if st.symbol not in config.OPTIONS["underlyings"]:
            return None
        ind = st.ind
        if not ind.or_done or ind.or_high is None or ind.or_low is None:
            return None
        deadline = clock.parse_hhmm(self.p["entry_deadline"],
                                    now.astimezone(clock.IST).date())
        if now >= deadline:
            return None
        bar = st.bars_5m[-1]
        or_range_pct = (ind.or_high - ind.or_low) / bar.close * 100.0
        if or_range_pct < self.p["min_or_range_pct"]:
            return None

        from bot import options as optmod
        chain = market.option_chains.get(st.symbol, [])
        if not chain:
            return None

        opt_type = None
        if bar.close > ind.or_high and \
                self.trades_today(st.symbol, "UP") < self.p["max_trades_per_direction"]:
            opt_type, direction = "CE", "UP"
        elif bar.close < ind.or_low and \
                self.trades_today(st.symbol, "DOWN") < self.p["max_trades_per_direction"]:
            opt_type, direction = "PE", "DOWN"
        if opt_type is None:
            return None

        contract = optmod.pick_option(chain, bar.close, opt_type)
        if contract is None:
            return None
        opt_st = market.get(contract.symbol)
        premium = opt_st.last_price if opt_st else None
        if not premium:
            return None
        self.note_entry(st.symbol, direction)   # count on the index, not the leg
        stop = premium * (1 - self.p["premium_stop_pct"] / 100.0)
        risk = premium - stop
        return Signal(self.name, contract.symbol, LONG, stop=stop,
                      target=premium + self.p["target_r"] * risk,
                      reason=f"{st.symbol} ORB {direction} → buy {contract.symbol.split(':')[1]}")

    def manage(self, pos: Position, st: SymbolState,
               now: datetime) -> ExitRequest | None:
        return None   # premium stop/target handled by the engine
