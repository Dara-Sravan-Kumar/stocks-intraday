"""The intraday indicator vocabulary an entry_expr may reference.

The whitelist is AUTO-DERIVED from the Snapshot dataclass: add a field here (and
populate it in build_snapshot) and every discovered/bred strategy can use it for
free. Fields flagged volume-dependent are None on index SymbolStates (indices
have no volume), so the DISCOVERED_OPT channel — whose signals fire on the index
itself — is restricted to the volume-free INDEX_VOCAB.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import ClassVar

from bot.state import MarketState, SymbolState


@dataclass(frozen=True)
class Snapshot:
    """Flat, scalar view of one instrument at one 5m bar — the entire vocabulary
    available to an entry_expr. All values are float | None; None means 'not yet
    computed' and makes any comparison touching it evaluate False (no entry)."""

    # -- price structure (computable for indices too — no volume required) -----
    close: float | None = None
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    or_high: float | None = None
    or_low: float | None = None
    or_range_pct: float | None = None        # (or_high-or_low)/close*100
    gap_pct: float | None = None
    day_change_pct: float | None = None       # (close-open)/open*100
    day_range_pct: float | None = None
    ema20: float | None = None
    rsi14: float | None = None
    rsi7: float | None = None
    rsi2: float | None = None
    atr14: float | None = None
    atr_pct: float | None = None              # atr14/close*100
    minutes_since_open: float | None = None

    # -- volume-dependent (None on indices) ------------------------------------
    vwap: float | None = None
    vwap_sigma: float | None = None
    vwap_dist_pct: float | None = None        # (close-vwap)/vwap*100
    rvol: float | None = None
    minutes_above_vwap: float | None = None
    minutes_below_vwap: float | None = None

    #: field names that are None on volume-less index states
    VOLUME_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "vwap", "vwap_sigma", "vwap_dist_pct", "rvol",
        "minutes_above_vwap", "minutes_below_vwap",
    })

    def as_env(self) -> dict[str, float | None]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


#: full vocabulary (equity underlyings have everything)
EQUITY_VOCAB: frozenset[str] = frozenset(f.name for f in fields(Snapshot))
#: index-safe subset (DISCOVERED_OPT signals fire on the index — no volume)
INDEX_VOCAB: frozenset[str] = EQUITY_VOCAB - Snapshot.VOLUME_FIELDS


def channel_vocab(channel: str) -> frozenset[str]:
    """DISCOVERED_OPT signals evaluate on the index (volume-free); DISCOVERED_EQ
    on stocks (full vocabulary)."""
    return INDEX_VOCAB if channel == "DISCOVERED_OPT" else EQUITY_VOCAB


def _pct(num: float | None, den: float | None) -> float | None:
    if num is None or not den:
        return None
    return num / den * 100.0


def build_snapshot(st: SymbolState, market: MarketState | None = None) -> Snapshot:
    """Read a live SymbolState into an immutable Snapshot. Pure; no I/O."""
    ind = st.ind
    close = st.last_price
    vwap = ind.vwap
    atr = ind.atr14.value
    return Snapshot(
        close=close,
        day_open=ind.day_open,
        day_high=ind.day_high,
        day_low=ind.day_low,
        or_high=ind.or_high,
        or_low=ind.or_low,
        or_range_pct=(_pct(ind.or_high - ind.or_low, close)
                      if ind.or_high is not None and ind.or_low is not None and close
                      else None),
        gap_pct=ind.gap_pct,
        day_change_pct=ind.day_change_pct,
        day_range_pct=ind.day_range_pct,
        ema20=ind.ema20.value,
        rsi14=ind.rsi14.value,
        rsi7=ind.rsi7.value,
        rsi2=ind.rsi2.value,
        atr14=atr,
        atr_pct=_pct(atr, close),
        minutes_since_open=float(ind.session_minutes) if ind.session_minutes else None,
        vwap=vwap,
        vwap_sigma=ind.vwap_sigma,
        vwap_dist_pct=(_pct(close - vwap, vwap) if close and vwap else None),
        rvol=ind.rvol(),
        minutes_above_vwap=float(ind.minutes_above_vwap) or None,
        minutes_below_vwap=float(ind.minutes_below_vwap) or None,
    )
