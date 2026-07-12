"""Feature 1 — multi-variant testing.

Two strategy VARIANTS may hold the SAME instrument at once so each builds its
own track record on the same scarce intraday setup; the one-per-instrument lock
is per (variant, symbol), and exits close positions by identity (never by
ticker), so with two legs on one ticker an exit closes only the right one.
"""
from __future__ import annotations

from datetime import datetime

import config
from bot.clock import IST
from bot.execution import LONG, Position
from bot.execution.paper_broker import PaperBroker
from bot.indicators import PrevDayLevels
from bot.risk import DayState, RiskEngine, Skip
from bot.state import SymbolState


def ts(h=10, m=0):
    return datetime(2026, 7, 6, h, m, tzinfo=IST)


def liquid_state(symbol="X") -> SymbolState:
    return SymbolState(symbol, PrevDayLevels(avg_1m_turnover=10_000_000))


def _leg(broker: PaperBroker, variant: str, symbol="RELIANCE") -> Position:
    return broker.open_position(
        "DISCOVERED_EQ", symbol, LONG, 10, 500.0, ts(), 495.0, 510.0,
        variant_key=variant,
    )


def test_two_variants_hold_one_ticker_concurrently():
    """A second variant is approved on a symbol variant-A already holds; the
    SAME variant is rejected (it can't double up on its own name)."""
    engine = RiskEngine()
    day = DayState(start_equity=100_000.0)
    held = Position("DISCOVERED_EQ", "RELIANCE", LONG, 10, ts(), 500.0, 495.0,
                    510.0, 200.0, 495.0, variant_key="disc_a")

    def ask(variant):
        return engine.approve(
            strategy="DISCOVERED_EQ", symbol="RELIANCE", entry_price=500.0,
            stop_price=495.0, sym_state=liquid_state("RELIANCE"),
            open_positions=[held], equity=100_000.0, margin_used=0.0,
            day=day, now=ts(), variant_key=variant,
        )

    # a DIFFERENT variant may still take the same instrument
    assert not isinstance(ask("disc_b"), Skip)
    # the SAME variant is locked out of doubling up
    res = ask("disc_a")
    assert isinstance(res, Skip) and "already holds" in res.reason


def test_exit_closes_only_the_matching_leg():
    """Two legs on one ticker (different variants). Closing one by identity
    leaves the other open and untouched — no close-by-ticker collision."""
    broker = PaperBroker(1_000_000.0)
    leg_a = _leg(broker, "disc_a")
    leg_b = _leg(broker, "disc_b")
    assert len(broker.positions) == 2
    assert {p.variant for p in broker.positions} == {"disc_a", "disc_b"}

    trade = broker.close_position(leg_a, 510.0, ts(11, 0), "TARGET")

    # only leg_a left the book; leg_b is still open and is the same object
    assert broker.positions == [leg_b]
    assert leg_b.variant == "disc_b"
    assert trade.position.variant == "disc_a"


def test_per_variant_position_cap():
    """MAX_POSITIONS_PER_STRATEGY is enforced per variant, not per family, so
    one busy variant never starves the others in the same channel."""
    engine = RiskEngine()
    day = DayState(start_equity=100_000.0)
    cap = config.MAX_POSITIONS_PER_STRATEGY
    # `cap` legs all belonging to variant disc_a, on distinct symbols
    held = [Position("DISCOVERED_EQ", f"S{i}", LONG, 1, ts(), 500.0, 495.0,
                     510.0, 50.0, 495.0, variant_key="disc_a")
            for i in range(cap)]

    def ask(variant, symbol):
        return engine.approve(
            strategy="DISCOVERED_EQ", symbol=symbol, entry_price=500.0,
            stop_price=495.0, sym_state=liquid_state(symbol),
            open_positions=held, equity=100_000.0, margin_used=0.0,
            day=day, now=ts(), variant_key=variant,
        )

    # disc_a is full...
    assert isinstance(ask("disc_a", "NEW"), Skip)
    # ...but disc_b in the same family still has its full budget
    assert not isinstance(ask("disc_b", "NEW"), Skip)
