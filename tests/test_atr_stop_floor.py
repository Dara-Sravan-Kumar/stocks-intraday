"""Task 3b — the ATR-based minimum-stop floor (widen noise-tight stops, never
past the max-risk ceiling, no-op when ATR is unavailable)."""
from __future__ import annotations

from bot.indicators import atr_stop_floor

MULT = 1.2


def _risk_pct(entry: float, stop: float) -> float:
    return abs(entry - stop) / entry * 100.0


def test_widens_a_noise_tight_long_stop():
    # entry 100, structure stop 0.1% away, ATR 0.5% -> floor to 1.2*0.5 = 0.6%.
    new = atr_stop_floor(100.0, 99.9, atr=0.5, side="LONG",
                         min_stop_atr_mult=MULT, max_risk_pct=2.0)
    assert round(_risk_pct(100.0, new), 4) == 0.6
    assert new < 99.9   # moved further from entry


def test_widens_a_noise_tight_short_stop_upward():
    new = atr_stop_floor(100.0, 100.1, atr=0.5, side="SHORT",
                         min_stop_atr_mult=MULT, max_risk_pct=2.0)
    assert round(_risk_pct(100.0, new), 4) == 0.6
    assert new > 100.1   # a short's stop sits above entry


def test_never_exceeds_max_risk_ceiling():
    # ATR is huge (2%) but the ceiling is 0.6% -> clamp at 0.6%.
    new = atr_stop_floor(100.0, 99.95, atr=2.0, side="LONG",
                         min_stop_atr_mult=MULT, max_risk_pct=0.6)
    assert round(_risk_pct(100.0, new), 4) == 0.6


def test_no_op_when_stop_already_beyond_floor():
    # stop already 2% away, ATR floor only asks for 0.6% -> unchanged.
    assert atr_stop_floor(100.0, 98.0, atr=0.5, side="LONG",
                          min_stop_atr_mult=MULT, max_risk_pct=5.0) == 98.0


def test_no_op_without_atr():
    assert atr_stop_floor(100.0, 99.9, atr=None, side="LONG",
                          min_stop_atr_mult=MULT, max_risk_pct=2.0) == 99.9
    assert atr_stop_floor(100.0, 99.9, atr=0.0, side="LONG",
                          min_stop_atr_mult=MULT, max_risk_pct=2.0) == 99.9


def test_no_op_when_disabled():
    assert atr_stop_floor(100.0, 99.9, atr=0.5, side="LONG",
                          min_stop_atr_mult=0.0, max_risk_pct=2.0) == 99.9
    assert atr_stop_floor(100.0, 99.9, atr=0.5, side="LONG",
                          min_stop_atr_mult=MULT, max_risk_pct=0.0) == 99.9


def test_no_op_on_missing_prices():
    assert atr_stop_floor(None, 99.9, atr=0.5, side="LONG",
                          min_stop_atr_mult=MULT, max_risk_pct=2.0) == 99.9
    assert atr_stop_floor(100.0, None, atr=0.5, side="LONG",
                          min_stop_atr_mult=MULT, max_risk_pct=2.0) is None


# --- engine wiring ----------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from bot.engine import Engine  # noqa: E402
from bot.execution import LONG  # noqa: E402
from bot.execution.paper_broker import PaperBroker  # noqa: E402
from bot.risk import RiskEngine  # noqa: E402
from bot.state import MarketState  # noqa: E402
from bot.strategies import Signal  # noqa: E402


class _Feed:
    def start(self): pass
    def stop(self): pass
    def poll(self): return []
    @property
    def exhausted(self): return False
    @property
    def source_name(self): return "replay"


def _engine():
    return Engine(mode="PAPER", feed=_Feed(), broker=PaperBroker(1e5),
                  strategies=[], risk=RiskEngine(), market=MarketState([], {}),
                  persist=False, require_fyers_feed=False)


def _st(atr=0.5):
    return SimpleNamespace(option_meta=None, bars_5m=[SimpleNamespace(close=100.0)],
                           ind=SimpleNamespace(atr14=SimpleNamespace(value=atr)))


def _sig(stop=99.9):
    return Signal("mom", "TEST", LONG, stop=stop, target=101.0, reason="x")


def test_engine_widens_classic_noise_tight_stop():
    strat = SimpleNamespace(use_atr_stop_floor=True, requires_options=False,
                            p={"max_risk_pct": 0.6})
    out = _engine()._apply_atr_stop_floor(_sig(), strat, _st(atr=0.5), "TEST")
    assert round(_risk_pct(100.0, out.stop), 4) == 0.6   # widened to the ATR floor


def test_engine_skips_when_strategy_opts_out():
    strat = SimpleNamespace(use_atr_stop_floor=False, requires_options=False, p={})
    out = _engine()._apply_atr_stop_floor(_sig(), strat, _st(), "TEST")
    assert out.stop == 99.9   # discovered channels keep their flat gated stop


def test_engine_skips_options_and_atr_unavailable():
    opt = SimpleNamespace(use_atr_stop_floor=True, requires_options=True, p={})
    assert _engine()._apply_atr_stop_floor(_sig(), opt, _st(), "TEST").stop == 99.9
    classic = SimpleNamespace(use_atr_stop_floor=True, requires_options=False, p={})
    assert _engine()._apply_atr_stop_floor(_sig(), classic, _st(atr=None),
                                           "TEST").stop == 99.9
