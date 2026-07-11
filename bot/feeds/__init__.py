"""Feed interface. Feeds emit COMPLETED 1m bars only.

Index updates arrive as Bars whose symbol is 'NIFTY' / 'BANKNIFTY'; the engine
routes those to MarketState.indices instead of strategies.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from bot.bars import Bar


class Feed(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def poll(self) -> list[Bar]:
        """Completed 1m bars since last poll, oldest first. May be empty."""

    @property
    def exhausted(self) -> bool:
        """True when no more data will ever come (replay finished)."""
        return False

    @property
    def source_name(self) -> str:
        return type(self).__name__
