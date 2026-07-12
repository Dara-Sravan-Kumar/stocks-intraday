"""Paper broker: simulated fills with slippage, realistic MIS costs, margin book.

Fill model (documented assumptions):
- Entries fill at the caller's reference price (next 1m bar open) worsened by
  SLIPPAGE_BPS.
- Stop/target exits are checked by the engine against bar high/low; the fill
  reference is the stop/target level, or the bar open when the bar gapped
  through the level (gap-through logic in exit_fill_price).
- Costs are charged once per round trip at close time.
"""
from __future__ import annotations

from datetime import datetime

import config
from bot import costs as costmod
from bot.bars import Bar
from bot.execution import LONG, Broker, ClosedTrade, Position


class PaperBroker(Broker):
    def __init__(self, starting_cash: float | None = None):
        self.starting_cash = starting_cash if starting_cash is not None \
            else config.PAPER_STARTING_CASH
        self.realized_pnl = 0.0
        self.positions: list[Position] = []

    # -- book ---------------------------------------------------------------

    @property
    def margin_used(self) -> float:
        return sum(p.margin_used for p in self.positions)

    def equity(self, marks: dict[str, float]) -> float:
        unreal = sum(
            p.unrealized(marks.get(p.symbol, p.entry_price)) for p in self.positions
        )
        return self.starting_cash + self.realized_pnl + unreal

    def cash_free(self, marks: dict[str, float]) -> float:
        return self.equity(marks) - self.margin_used

    # -- fills --------------------------------------------------------------

    def open_position(self, strategy: str, symbol: str, side: str, qty: int,
                      ref_price: float, ts: datetime, stop: float,
                      target: float | None, margin: float | None = None,
                      instrument: str = "EQ",
                      variant_key: str = "") -> Position | None:
        fill = costmod.slippage_price(ref_price, side_is_buy=(side == LONG),
                                      instrument=instrument)
        if margin is None:
            margin = fill * qty / config.INTRADAY_LEVERAGE
        pos = Position(
            strategy=strategy, symbol=symbol, side=side, qty=qty,
            entry_ts=ts, entry_price=fill, stop_price=stop,
            target_price=target, margin_used=margin, planned_stop=stop,
            instrument=instrument, variant_key=variant_key or strategy,
        )
        self.positions.append(pos)
        return pos

    def close_position(self, pos: Position, ref_price: float, ts: datetime,
                       reason: str) -> ClosedTrade:
        exit_is_buy = not pos.is_long
        fill = costmod.slippage_price(ref_price, side_is_buy=exit_is_buy,
                                      instrument=pos.instrument)
        if pos.is_long:
            buy_value = pos.entry_price * pos.qty
            sell_value = fill * pos.qty
            gross = sell_value - buy_value
        else:
            sell_value = pos.entry_price * pos.qty
            buy_value = fill * pos.qty
            gross = sell_value - buy_value
        if pos.instrument == "OPT":
            cost = costmod.options_costs(buy_value, sell_value)["total"]
        else:
            cost = costmod.intraday_costs(buy_value, sell_value)["total"]
        net = gross - cost
        self.realized_pnl += net
        if pos in self.positions:
            self.positions.remove(pos)
        risk = pos.risk_per_share * pos.qty
        r_mult = (gross / risk) if risk > 0 else None
        return ClosedTrade(
            position=pos, exit_ts=ts, exit_price=fill, gross_pnl=gross,
            costs=cost, net_pnl=net, r_multiple=r_mult, exit_reason=reason,
        )


def stop_hit(pos: Position, bar: Bar) -> bool:
    return bar.low <= pos.stop_price if pos.is_long else bar.high >= pos.stop_price


def target_hit(pos: Position, bar: Bar) -> bool:
    if pos.target_price is None:
        return False
    return bar.high >= pos.target_price if pos.is_long else bar.low <= pos.target_price


def exit_fill_price(pos: Position, bar: Bar, level: float) -> float:
    """Fill reference for a stop/target exit, honoring gaps through the level.

    If the bar OPENED beyond the level (gap), the realistic fill is the open,
    not the level. Conservative for stops, honest for targets.
    """
    if pos.is_long:
        if level <= pos.stop_price:            # stop side for a long
            return min(level, bar.open)
        return bar.open if bar.open >= level else level
    # short position
    if level >= pos.stop_price:                # stop side for a short
        return max(level, bar.open)
    return bar.open if bar.open <= level else level
