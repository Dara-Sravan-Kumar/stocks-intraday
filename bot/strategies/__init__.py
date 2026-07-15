"""Strategy interface. Strategies are pure: they read SymbolState/MarketState
and return Signal / ExitRequest objects. They never touch feeds, brokers,
wall-clock, or the DB. Params come exclusively from config.STRATEGY_PARAMS.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import config
from bot import clock
from bot.execution import Position
from bot.state import MarketState, SymbolState


@dataclass(frozen=True)
class Signal:
    strategy: str            # managing channel/family (routes manage()/note_entry)
    symbol: str
    side: str                # LONG | SHORT
    stop: float
    target: float | None
    reason: str
    variant_key: str | None = None   # track-record identity; None -> == strategy

    @property
    def variant(self) -> str:
        """Attribution + uniqueness identity. Classic strategies: == strategy.
        Discovered specs: the spec key, so many specs share one managing class
        yet each keeps its own ledger and its own one-per-instrument lock."""
        return self.variant_key or self.strategy


@dataclass(frozen=True)
class ExitRequest:
    reason: str              # becomes trades.exit_reason
    soft: bool = False       # True -> a "setup broken" exit that reads the
                             # instrument's ABSOLUTE state (below-VWAP, RSI left
                             # oversold, structure broken). The engine suppresses
                             # these until the position has been held
                             # MIN_HOLD_BARS_BEFORE_SOFT_EXIT bars, so a state
                             # that was already broken at entry can't guillotine
                             # the trade on bar 1. Time stops and stop/target
                             # hits are NOT soft — they fire on any bar.


class Strategy(ABC):
    name: str = "base"
    requires_options: bool = False   # True -> only loaded in run_live --options
    # Apply the ATR-based minimum-stop floor (engine-side) to this strategy's
    # signals. True for the classic strategies, whose stops are derived from
    # support/structure and can sit inside per-bar noise. The discovered channels
    # use a deliberate flat-% stop the gate replays exactly, so they opt out to
    # keep live and gate stops identical.
    use_atr_stop_floor: bool = True

    def __init__(self, params: dict | None = None):
        self.p = params if params is not None else config.STRATEGY_PARAMS[self.name]
        self._trades_today: dict[tuple[str, str], int] = {}   # (symbol, side) -> n

    # -- lifecycle ------------------------------------------------------------

    def on_session_start(self) -> None:
        self._trades_today = {}

    def note_entry(self, symbol: str, side: str) -> None:
        key = (symbol, side)
        self._trades_today[key] = self._trades_today.get(key, 0) + 1

    def trades_today(self, symbol: str, side: str | None = None) -> int:
        if side is not None:
            return self._trades_today.get((symbol, side), 0)
        return sum(n for (sym, _), n in self._trades_today.items() if sym == symbol)

    # -- helpers ----------------------------------------------------------------

    def in_window(self, now: datetime, start_key: str = "entry_start",
                  end_key: str = "entry_end") -> bool:
        d = now.astimezone(clock.IST).date()
        start = clock.parse_hhmm(self.p[start_key], d) if start_key in self.p else None
        end = clock.parse_hhmm(self.p[end_key], d) if end_key in self.p else None
        if start and now < start:
            return False
        if end and now >= end:
            return False
        return True

    # -- core -------------------------------------------------------------------

    @abstractmethod
    def on_bar_5m(self, st: SymbolState, market: MarketState,
                  now: datetime) -> Signal | None:
        """Called on each completed 5m bar for each symbol. Return a Signal to enter."""

    def manage(self, pos: Position, st: SymbolState,
               now: datetime) -> ExitRequest | None:
        """Called every 1m bar for this strategy's open positions.

        May mutate pos.stop_price (trailing) / pos.scratch, or return an
        ExitRequest for time stops and discretionary exits. Stop/target hits
        are handled by the engine.
        """
        return None


def build_strategies(names: list[str] | None = None,
                     options_mode: bool = False) -> list[Strategy]:
    """Instantiate enabled strategies from the registry (optionally filtered).

    Equity sessions load equity strategies; --options sessions load only the
    options strategies. An explicit names list overrides both filters.
    """
    from bot.strategies.orb import Orb
    from bot.strategies.vwap_reversion import VwapReversion
    from bot.strategies.vwap_pullback import VwapPullback
    from bot.strategies.momentum_breakout import MomentumBreakout
    from bot.strategies.gap import Gap
    from bot.strategies.rsi2_scalp import Rsi2Scalp
    from bot.strategies.trend_day import TrendDay
    from bot.strategies.range_fade import RangeFade
    from bot.strategies.opt_orb import OptOrb
    from bot.strategies.opt_trend_day import OptTrendDay
    from bot.strategies.opt_straddle import OptStraddle

    registry: dict[str, type[Strategy]] = {
        cls.name: cls
        for cls in (Orb, VwapReversion, VwapPullback, MomentumBreakout, Gap,
                    Rsi2Scalp, TrendDay, RangeFade,
                    OptOrb, OptTrendDay, OptStraddle)
    }
    out: list[Strategy] = []
    for name, cls in registry.items():
        if names is not None:
            if name in names:          # explicit request overrides all filters
                out.append(cls())
            continue
        if cls.requires_options != options_mode:
            continue
        if config.STRATEGY_PARAMS.get(name, {}).get("enabled", False):
            out.append(cls())

    if names is None and getattr(config, "DISCOVERY_ENABLED", False):
        out.extend(_build_discovered(options_mode))
    return out


def _build_discovered(options_mode: bool) -> list[Strategy]:
    """Load ACTIVE discovered specs into their channel's ExprStrategy. Fully
    guarded — a discovery failure must never stop the classic fleet from loading."""
    try:
        from bot.discovery.registry import load_active_specs
        from bot.strategies.discovered import DiscoveredEquity, DiscoveredOptions
        cls = DiscoveredOptions if options_mode else DiscoveredEquity
        specs = load_active_specs(cls.channel)
        return [cls(specs)]
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("discovered channel load failed", exc_info=True)
        return []
