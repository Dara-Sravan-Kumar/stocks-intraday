"""Execution layer: order/position primitives and the Broker interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

LONG = "LONG"
SHORT = "SHORT"


@dataclass
class Position:
    """Runtime position. Mirrors the positions table plus strategy scratch state."""
    strategy: str
    symbol: str
    side: str                 # LONG | SHORT
    qty: int
    entry_ts: datetime
    entry_price: float
    stop_price: float
    target_price: float | None
    margin_used: float
    planned_stop: float       # original stop, used for R multiples
    db_id: int | None = None
    mode: str = "PAPER"
    instrument: str = "EQ"    # EQ | OPT — selects cost model and slippage
    scratch: dict = field(default_factory=dict)   # strategy-managed (trails etc.)

    @property
    def is_long(self) -> bool:
        return self.side == LONG

    def unrealized(self, last_price: float) -> float:
        diff = last_price - self.entry_price
        return diff * self.qty if self.is_long else -diff * self.qty

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry_price - self.planned_stop)


@dataclass
class ClosedTrade:
    position: Position
    exit_ts: datetime
    exit_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    r_multiple: float | None
    exit_reason: str


class Broker(ABC):
    """Executes fills. Holds the book (cash / margin / open positions)."""

    @abstractmethod
    def open_position(self, strategy: str, symbol: str, side: str, qty: int,
                      ref_price: float, ts: datetime, stop: float,
                      target: float | None, margin: float | None = None,
                      instrument: str = "EQ") -> Position | None: ...

    @abstractmethod
    def close_position(self, pos: Position, ref_price: float, ts: datetime,
                       reason: str) -> ClosedTrade: ...

    @abstractmethod
    def equity(self, marks: dict[str, float]) -> float: ...
