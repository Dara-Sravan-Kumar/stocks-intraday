"""Portfolio risk engine: sizing, limits, daily-loss halt, circuit breaker.

Every rejection is a Skip(reason) — persisted by the engine so the user can
always see WHY a signal didn't become a trade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import config
from bot.execution import Position
from bot.state import MarketState, SymbolState


@dataclass
class Skip:
    reason: str


@dataclass
class Approval:
    qty: int
    margin: float
    risk_amount: float


@dataclass
class DayState:
    """Per-session mutable counters, reset each morning."""
    start_equity: float
    halted: bool = False
    halt_reason: str = ""
    circuit_paused_until: datetime | None = None
    circuit_trips: int = 0
    entries_today: int = 0
    trades_by_strategy: dict[str, int] = field(default_factory=dict)
    consecutive_losses: dict[str, int] = field(default_factory=dict)
    benched_strategies: set[str] = field(default_factory=set)

    def record_trade_result(self, strategy: str, net_pnl: float) -> None:
        self.trades_by_strategy[strategy] = self.trades_by_strategy.get(strategy, 0) + 1
        if net_pnl < 0:
            n = self.consecutive_losses.get(strategy, 0) + 1
            self.consecutive_losses[strategy] = n
            if n >= config.CONSECUTIVE_LOSSES_TO_BENCH:
                self.benched_strategies.add(strategy)
        else:
            self.consecutive_losses[strategy] = 0


class RiskEngine:
    def __init__(self, risk_per_trade_pct: float | None = None,
                 max_concurrent: int | None = None):
        self.risk_per_trade_pct = risk_per_trade_pct or config.RISK_PER_TRADE_PCT
        self.max_concurrent = max_concurrent or config.MAX_CONCURRENT_POSITIONS

    # -- circuit breaker ------------------------------------------------------

    def check_circuit_breaker(self, market: MarketState, day: DayState,
                              now: datetime) -> str | None:
        """Trips the pause when an index moves violently. Returns reason if tripped now."""
        if day.circuit_paused_until and now < day.circuit_paused_until:
            return None  # already paused
        for name, idx in market.indices.items():
            m15 = idx.move_pct_last(15, now)
            mo = idx.move_pct_from_open()
            reason = None
            if m15 is not None and abs(m15) >= config.CIRCUIT_INDEX_MOVE_15M_PCT:
                reason = f"{name} moved {m15:+.2f}% in 15m"
            elif mo is not None and abs(mo) >= config.CIRCUIT_INDEX_MOVE_OPEN_PCT:
                reason = f"{name} moved {mo:+.2f}% from open"
            if reason:
                day.circuit_trips += 1
                if day.circuit_trips >= 2:
                    day.circuit_paused_until = now + timedelta(hours=12)  # rest of day
                    return f"circuit breaker (2nd trip, halted for day): {reason}"
                day.circuit_paused_until = now + timedelta(
                    minutes=config.CIRCUIT_PAUSE_MINUTES
                )
                return f"circuit breaker: {reason}"
        return None

    # -- daily loss -----------------------------------------------------------

    def daily_loss_breached(self, equity_now: float, day: DayState) -> bool:
        if day.start_equity <= 0:
            return False
        loss_pct = (day.start_equity - equity_now) / day.start_equity * 100.0
        return loss_pct >= config.MAX_DAILY_LOSS_PCT

    # -- regime filter ----------------------------------------------------------

    @staticmethod
    def regime_allows(side: str, market: MarketState) -> bool:
        """Trade with the index, never against it. Unknown regime -> allow."""
        if not config.REGIME_FILTER_ENABLED:
            return True
        nifty = market.indices.get("NIFTY")
        if nifty is None:
            return True
        move = nifty.move_pct_from_open()
        if move is None:
            return True
        return move >= 0 if side == "LONG" else move <= 0

    # -- entry approval ---------------------------------------------------------

    def approve(self, *, strategy: str, symbol: str, entry_price: float,
                stop_price: float, sym_state: SymbolState,
                open_positions: list[Position], equity: float,
                margin_used: float, day: DayState,
                now: datetime, side: str = "LONG") -> Approval | Skip:
        if day.halted:
            return Skip(f"day halted: {day.halt_reason}")
        if day.entries_today >= config.MAX_ENTRIES_PER_DAY:
            return Skip(f"portfolio entry budget ({config.MAX_ENTRIES_PER_DAY}/day) spent")
        if day.circuit_paused_until and now < day.circuit_paused_until:
            return Skip("circuit breaker pause active")
        if strategy in day.benched_strategies:
            return Skip(f"{strategy} benched after "
                        f"{config.CONSECUTIVE_LOSSES_TO_BENCH} consecutive losses")
        if day.trades_by_strategy.get(strategy, 0) >= config.MAX_TRADES_PER_DAY_PER_STRATEGY:
            return Skip(f"{strategy} hit max trades/day")

        if len(open_positions) >= self.max_concurrent:
            return Skip("max concurrent positions")
        if sum(1 for p in open_positions if p.strategy == strategy) \
                >= config.MAX_POSITIONS_PER_STRATEGY:
            return Skip(f"{strategy} at max positions")
        if sum(1 for p in open_positions if p.symbol == symbol) \
                >= config.MAX_POSITIONS_PER_SYMBOL:
            return Skip(f"{symbol} already has a position")

        is_option = sym_state.option_meta is not None
        if not is_option:
            if not (config.MIN_PRICE <= entry_price <= config.MAX_PRICE):
                return Skip(f"price {entry_price:.2f} outside allowed range")
            turnover = sym_state.avg_1m_turnover()
            if turnover is not None and turnover < config.MIN_AVG_1M_TURNOVER:
                return Skip(f"illiquid: avg 1m turnover ₹{turnover:,.0f}")
        elif entry_price <= 0:
            return Skip("no premium quote yet")

        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            return Skip("zero risk distance (stop == entry)")
        if not is_option and \
                risk_per_share / entry_price * 100.0 < config.MIN_STOP_DISTANCE_PCT:
            return Skip(f"stop {risk_per_share / entry_price * 100.0:.2f}% too tight "
                        f"vs ~0.1% round-trip costs")
        risk_amount = equity * self.risk_per_trade_pct / 100.0
        qty = math.floor(risk_amount / risk_per_share)

        margin_cap = equity * config.MAX_MARGIN_PCT / 100.0
        if is_option:
            lot = max(1, sym_state.lot_size)
            qty = (qty // lot) * lot
            if qty < lot:
                return Skip(f"risk budget < 1 lot ({lot}) at this premium stop")
            underlying = sym_state.option_meta.underlying
            per_lot_short = config.OPTIONS["short_margin_per_lot"].get(underlying, 150_000.0)
            is_short = side == "SHORT"

            def option_margin(q: int) -> float:
                return per_lot_short * (q // lot) if is_short else entry_price * q

            while qty >= lot and margin_used + option_margin(qty) > margin_cap:
                qty -= lot
            if qty < lot:
                return Skip("margin cap: can't carry even one lot "
                            f"({'short' if is_short else 'long'} "
                            f"needs ₹{option_margin(lot):,.0f})")
            margin_needed = option_margin(qty)
        else:
            max_notional = equity * config.MAX_NOTIONAL_PCT / 100.0
            if qty * entry_price > max_notional:
                qty = math.floor(max_notional / entry_price)
            margin_needed = qty * entry_price / config.INTRADAY_LEVERAGE
            if margin_used + margin_needed > margin_cap:
                afford = (margin_cap - margin_used) * config.INTRADAY_LEVERAGE
                qty = min(qty, math.floor(afford / entry_price))
                margin_needed = qty * entry_price / config.INTRADAY_LEVERAGE
            if qty < config.MIN_QTY:
                return Skip("qty=0 after sizing caps")

        return Approval(qty=qty, margin=margin_needed,
                        risk_amount=qty * risk_per_share)
