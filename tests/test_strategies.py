from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bot.bars import Bar
from bot.clock import IST
from bot.execution import LONG, SHORT, Position
from bot.indicators import PrevDayLevels
from bot.state import MarketState, SymbolState
from bot.strategies import ExitRequest, build_strategies
from bot.strategies.gap import Gap
from bot.strategies.momentum_breakout import MomentumBreakout
from bot.strategies.orb import Orb
from bot.strategies.rsi2_scalp import Rsi2Scalp
from bot.strategies.vwap_pullback import VwapPullback
from bot.strategies.vwap_reversion import VwapReversion

MARKET = MarketState(["X"])


def ts(h, m):
    return datetime(2026, 7, 6, h, m, tzinfo=IST)


def bar5(o, h, l, c, v=10_000, at=None):  # noqa: E741
    at = at or ts(10, 0)
    return Bar("X", at, o, h, l, c, v, interval=5)


def test_build_strategies_registry():
    strats = build_strategies()
    # default = enabled-only (rsi2_scalp / vwap_reversion ship disabled)
    assert {s.name for s in strats} == {
        "orb", "vwap_pullback", "momentum_breakout", "gap",
    }
    only = build_strategies(["orb"])
    assert [s.name for s in only] == ["orb"]
    # explicit request overrides the enabled flag (research use)
    research = build_strategies(["rsi2_scalp"])
    assert [s.name for s in research] == ["rsi2_scalp"]


# --------------------------------------------------------------------- ORB

def orb_state(breakout_vol=30_000):
    st = SymbolState("X")
    # opening range 09:15-09:29: 500-505
    for i in range(15):
        st.on_bar_1m(Bar("X", ts(9, 15) + timedelta(minutes=i),
                         502, 505 if i == 3 else 503, 500 if i == 7 else 501, 502, 1000))
    # breakout 5m bar 09:30-09:34 closing above OR high on volume
    for i in range(5):
        px = 505 + i * 0.6
        st.on_bar_1m(Bar("X", ts(9, 30) + timedelta(minutes=i),
                         px, px + 0.5, px - 0.5, px + 0.5, breakout_vol // 5))
    st.flush_5m()
    return st


def test_orb_fires_long_on_volume_breakout():
    strat = Orb()
    strat.on_session_start()
    st = orb_state(breakout_vol=30_000)   # mean OR 5m vol = 5000; 30k >= 1.5x
    sig = strat.on_bar_5m(st, MARKET, ts(9, 35))
    assert sig is not None and sig.side == LONG
    assert sig.stop == pytest.approx((505 + 500) / 2)
    assert sig.target > st.bars_5m[-1].close


def test_orb_skips_without_volume():
    strat = Orb()
    strat.on_session_start()
    st = orb_state(breakout_vol=5_000)    # below 1.5x mean OR volume
    assert strat.on_bar_5m(st, MARKET, ts(9, 35)) is None


def test_orb_respects_deadline_and_trade_cap():
    strat = Orb()
    strat.on_session_start()
    st = orb_state()
    assert strat.on_bar_5m(st, MARKET, ts(13, 5)) is None   # past deadline
    strat.note_entry("X", LONG)
    assert strat.on_bar_5m(st, MARKET, ts(9, 40)) is None   # cap 1/day/direction


# ----------------------------------------------------------- VWAP reversion

def reversion_state(close, vwap=100.0, sigma=0.5, rsi=75.0, day_open=None):
    st = SymbolState("X")
    st.bars_5m.append(bar5(close - 0.5, close + 0.2, close - 0.6, close))
    st.ind.vwap = vwap
    st.ind.vwap_sigma = sigma
    st.ind.rsi14.value = rsi
    st.ind.day_open = day_open if day_open is not None else close * 0.999
    st.ind.last_close = close
    return st


def test_vwap_reversion_fires_short_above_band():
    strat = VwapReversion()
    strat.on_session_start()
    st = reversion_state(close=101.2, vwap=100, sigma=0.45, rsi=75)  # > +2.5σ
    sig = strat.on_bar_5m(st, MARKET, ts(11, 0))
    assert sig is not None and sig.side == SHORT
    assert sig.target == pytest.approx(100.0)
    assert sig.stop > 101.2


def test_vwap_reversion_skips_trend_day_and_window():
    strat = VwapReversion()
    strat.on_session_start()
    trend = reversion_state(close=103, vwap=100, sigma=0.5, rsi=75, day_open=100)
    assert strat.on_bar_5m(trend, MARKET, ts(11, 0)) is None   # day +3% -> trend
    ok = reversion_state(close=101.2)
    assert strat.on_bar_5m(ok, MARKET, ts(9, 40)) is None      # before 10:00
    assert strat.on_bar_5m(ok, MARKET, ts(14, 35)) is None     # after 14:30


def test_vwap_reversion_time_stop():
    strat = VwapReversion()
    pos = Position("vwap_reversion", "X", SHORT, 10, ts(10, 0), 101, 102, 100, 200, 102)
    st = reversion_state(close=100.8)
    assert strat.manage(pos, st, ts(10, 30)) is None
    req = strat.manage(pos, st, ts(11, 1))
    assert isinstance(req, ExitRequest) and req.reason == "TIME"


# ----------------------------------------------------------- VWAP pullback

def pullback_state(long_side=True):
    st = SymbolState("X")
    vwap = 100.0
    st.ind.vwap = vwap
    st.ind._vwap_track = [99.0, 99.2, 99.4, 99.6, 99.8, 99.9, 100.0] if long_side \
        else [101.0, 100.8, 100.6, 100.4, 100.2, 100.1, 100.0]
    st.ind.minutes_above_vwap = 60 if long_side else 0
    st.ind.minutes_below_vwap = 0 if long_side else 60
    st.ind.day_open = 99.0 if long_side else 101.0
    if long_side:
        st.bars_5m.append(bar5(100.4, 100.5, 99.95, 100.3))   # tags vwap, closes above
        st.ind.last_close = 100.3
    else:
        st.bars_5m.append(bar5(99.6, 100.05, 99.5, 99.7))     # tags vwap, closes below
        st.ind.last_close = 99.7
    return st


def test_vwap_pullback_long_fires():
    strat = VwapPullback()
    strat.on_session_start()
    st = pullback_state(long_side=True)
    sig = strat.on_bar_5m(st, MARKET, ts(11, 0))
    assert sig is not None and sig.side == LONG
    assert sig.stop < 99.95


def test_vwap_pullback_short_fires():
    strat = VwapPullback()
    strat.on_session_start()
    st = pullback_state(long_side=False)
    sig = strat.on_bar_5m(st, MARKET, ts(11, 0))
    assert sig is not None and sig.side == SHORT


def test_vwap_pullback_needs_trend_side_time():
    strat = VwapPullback()
    strat.on_session_start()
    st = pullback_state(long_side=True)
    st.ind.minutes_above_vwap = 10   # under min_side_minutes
    assert strat.on_bar_5m(st, MARKET, ts(11, 0)) is None


def test_vwap_pullback_breakeven_management():
    strat = VwapPullback()
    pos = Position("vwap_pullback", "X", LONG, 10, ts(10, 0),
                   100.0, 99.4, 101.2, 200, 99.4)
    st = pullback_state(long_side=True)
    st.bars_1m.append(Bar("X", ts(10, 30), 100.7, 100.7, 100.6, 100.65, 100))
    strat.manage(pos, st, ts(10, 30))
    assert pos.stop_price == pytest.approx(100.0)   # moved to breakeven
    assert pos.scratch["breakeven_done"]


# ------------------------------------------------------ momentum breakout

def momentum_state(close=520.0, rvol_mult=3.0, pdh=515.0, pdl=490.0):
    profile = [10_000.0 * (i + 1) for i in range(375)]
    st = SymbolState("X", PrevDayLevels(
        high=pdh, low=pdl, close=505.0, avg_daily_range_pct=2.0,
        avg_cum_volume=profile, avg_1m_turnover=5_000_000,
    ))
    st.ind.day_open = 508.0
    st.ind.day_high = close
    st.ind.day_low = 506.0
    st.ind.last_close = close
    st.ind.session_minutes = 60
    st.ind.cum_volume = int(profile[59] * rvol_mult)
    st.bars_5m.append(bar5(close - 2, close + 0.5, close - 2.5, close))
    return st


def test_momentum_breakout_long_fires():
    strat = MomentumBreakout()
    strat.on_session_start()
    st = momentum_state()
    sig = strat.on_bar_5m(st, MARKET, ts(10, 30))
    assert sig is not None and sig.side == LONG
    assert sig.stop < 520.0


def test_momentum_breakout_needs_rvol():
    strat = MomentumBreakout()
    strat.on_session_start()
    st = momentum_state(rvol_mult=1.0)
    assert strat.on_bar_5m(st, MARKET, ts(10, 30)) is None


def test_momentum_breakout_skips_extended_day():
    strat = MomentumBreakout()
    strat.on_session_start()
    st = momentum_state()
    st.ind.day_low = 495.0    # day range ~4.8% vs avg 2% -> >2x, extended
    assert strat.on_bar_5m(st, MARKET, ts(10, 30)) is None


def test_momentum_trail_moves_stop_with_ema():
    strat = MomentumBreakout()
    pos = Position("momentum_breakout", "X", LONG, 10, ts(10, 0),
                   520.0, 517.0, 526.0, 1000, 517.0)
    st = momentum_state()
    st.ind.ema20.value = 523.0
    st.bars_1m.append(Bar("X", ts(11, 0), 524, 524.5, 523.5, 524.0, 100))
    strat.manage(pos, st, ts(11, 0))
    assert pos.stop_price == pytest.approx(523.0)


# ------------------------------------------------------------------- gap

def gap_state(gap_pct, first_bull, trigger_above_first=True):
    prev_close = 500.0
    day_open = prev_close * (1 + gap_pct / 100.0)
    st = SymbolState("X", PrevDayLevels(close=prev_close))
    st.ind.day_open = day_open
    first = Bar("X", ts(9, 15),
                day_open, day_open + 1.5, day_open - 1.5,
                day_open + (1.0 if first_bull else -1.0), 10_000, 5)
    st.bars_5m.append(first)
    if trigger_above_first:
        trig_close = first.high + 1.0
    else:
        trig_close = first.low - 0.4   # below first-bar low, above the gap-fill target
    trig = Bar("X", ts(9, 25), first.close, max(first.close, trig_close) + 0.3,
               min(first.close, trig_close) - 0.3, trig_close, 12_000, 5)
    st.bars_5m.append(trig)
    st.ind.day_high = max(first.high, trig.high)
    st.ind.day_low = min(first.low, trig.low)
    st.ind.last_close = trig_close
    return st


def test_gap_and_go_long():
    strat = Gap()
    strat.on_session_start()
    st = gap_state(gap_pct=1.5, first_bull=True, trigger_above_first=True)
    sig = strat.on_bar_5m(st, MARKET, ts(9, 30))
    assert sig is not None and sig.side == LONG
    assert sig.stop == pytest.approx(st.bars_5m[0].low)


def test_gap_fade_short():
    strat = Gap()
    strat.on_session_start()
    st = gap_state(gap_pct=0.5, first_bull=False, trigger_above_first=False)
    sig = strat.on_bar_5m(st, MARKET, ts(9, 30))
    assert sig is not None and sig.side == SHORT
    assert sig.target == pytest.approx(500.0)   # gap fill to prev close


def test_gap_ignores_tiny_and_huge_gaps():
    strat = Gap()
    strat.on_session_start()
    tiny = gap_state(gap_pct=0.1, first_bull=True)
    assert strat.on_bar_5m(tiny, MARKET, ts(9, 30)) is None
    huge = gap_state(gap_pct=5.0, first_bull=True)
    assert strat.on_bar_5m(huge, MARKET, ts(9, 30)) is None


def test_gap_window_closes_at_1030():
    strat = Gap()
    strat.on_session_start()
    st = gap_state(gap_pct=1.5, first_bull=True)
    assert strat.on_bar_5m(st, MARKET, ts(10, 35)) is None


# ------------------------------------------------------------ RSI2 scalp

def rsi2_state(rsi, close, vwap):
    st = SymbolState("X")
    st.bars_5m.append(bar5(close - 0.3, close + 0.1, close - 0.4, close))
    st.ind.rsi2.value = rsi
    st.ind.vwap = vwap
    return st


def test_rsi2_long_above_vwap():
    strat = Rsi2Scalp()
    strat.on_session_start()
    st = rsi2_state(rsi=3, close=101, vwap=100)
    sig = strat.on_bar_5m(st, MARKET, ts(11, 0))
    assert sig is not None and sig.side == LONG


def test_rsi2_no_countertrend():
    strat = Rsi2Scalp()
    strat.on_session_start()
    st = rsi2_state(rsi=3, close=99, vwap=100)   # oversold but BELOW vwap
    assert strat.on_bar_5m(st, MARKET, ts(11, 0)) is None


def test_rsi2_exit_on_rsi_recovery_and_time():
    strat = Rsi2Scalp()
    pos = Position("rsi2_scalp", "X", LONG, 10, ts(10, 0), 101, 100.6, 101.5, 200, 100.6)
    st = rsi2_state(rsi=65, close=101.2, vwap=100)
    req = strat.manage(pos, st, ts(10, 10))
    assert isinstance(req, ExitRequest) and req.reason == "RSI_EXIT"
    st2 = rsi2_state(rsi=30, close=101.0, vwap=100)
    req2 = strat.manage(pos, st2, ts(10, 50))
    assert isinstance(req2, ExitRequest) and req2.reason == "TIME"
