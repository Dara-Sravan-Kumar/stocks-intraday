from __future__ import annotations

from datetime import datetime, timedelta

from bot import daytype
from bot.bars import Bar
from bot.clock import IST
from bot.indicators import PrevDayLevels
from bot.state import MarketState, SymbolState
from bot.strategies.range_fade import RangeFade
from bot.strategies.trend_day import TrendDay
from bot.execution import LONG, SHORT


def ts(h, m):
    return datetime(2026, 7, 6, h, m, tzinfo=IST)


def market_with_nifty(move_pct: float) -> MarketState:
    m = MarketState(["X"])
    m.on_index_tick("NIFTY", ts(9, 15), 25_000.0)
    m.on_index_tick("NIFTY", ts(11, 0), 25_000.0 * (1 + move_pct / 100))
    return m


def trend_up_state() -> SymbolState:
    st = SymbolState("X", PrevDayLevels(avg_daily_range_pct=1.5,
                                        avg_1m_turnover=5_000_000))
    ind = st.ind
    ind.day_open = 500.0
    ind.day_low = 499.0
    ind.day_high = 506.0
    ind.last_close = 505.5           # +1.1% day, near highs
    ind.vwap = 503.0
    ind._vwap_track = [500.5, 501.0, 501.8, 502.4, 503.0]
    ind.session_minutes = 120
    st.bars_5m.append(Bar("X", ts(11, 0), 504.5, 505.8, 504.0, 505.5, 50_000, 15))
    return st


def range_state() -> SymbolState:
    st = SymbolState("X", PrevDayLevels(avg_daily_range_pct=1.5,
                                        avg_1m_turnover=5_000_000))
    ind = st.ind
    ind.day_open = 500.0
    ind.day_low = 498.5
    ind.day_high = 501.5             # 0.6% range vs 1.5% avg -> compressed
    ind.last_close = 499.0           # -0.2% day, near range low
    ind.vwap = 500.6                 # far enough for the 0.30% reward gate
    ind.minutes_above_vwap = 60
    ind.minutes_below_vwap = 55
    ind.rsi7.value = 30.0
    st.bars_5m.append(Bar("X", ts(11, 30), 499.4, 499.6, 498.7, 499.0, 30_000, 15))
    return st


def test_classify_trend_up():
    st = trend_up_state()
    assert daytype.classify(st, market_with_nifty(+0.5)) == daytype.TREND_UP


def test_trend_needs_index_agreement():
    st = trend_up_state()
    assert daytype.classify(st, market_with_nifty(-0.5)) == daytype.UNKNOWN


def test_classify_trend_down_mirror():
    st = trend_up_state()
    ind = st.ind
    ind.day_low, ind.day_high = 494.0, 501.0
    ind.last_close = 494.5           # -1.1%, near lows
    ind.vwap = 496.5
    ind._vwap_track = [499.5, 499.0, 498.2, 497.6, 496.5]
    assert daytype.classify(st, market_with_nifty(-0.6)) == daytype.TREND_DOWN


def test_classify_range_day():
    st = range_state()
    assert daytype.classify(st, market_with_nifty(0.0)) == daytype.RANGE


def test_range_needs_time_on_both_sides():
    st = range_state()
    st.ind.minutes_below_vwap = 5    # one-sided -> not a proven range
    assert daytype.classify(st, market_with_nifty(0.0)) != daytype.RANGE


def test_ambiguous_is_unknown():
    st = trend_up_state()
    st.ind.last_close = 502.0        # mid-range, modest change
    st.ind.day_high = 506.0
    assert daytype.classify(st, market_with_nifty(0.1)) == daytype.UNKNOWN


# --------------------------------------------------------------- strategies

def test_trend_day_long_signal_and_vwap_trail():
    strat = TrendDay()
    strat.on_session_start()
    st = trend_up_state()
    profile = [10_000.0 * (i + 1) for i in range(375)]
    st.ind.prev_day.avg_cum_volume = profile
    st.ind.cum_volume = int(profile[119] * 2)   # rvol 2.0
    sig = strat.on_bar_5m(st, market_with_nifty(+0.5), ts(11, 0))
    assert sig is not None and sig.side == LONG
    assert sig.target is None                    # ride, don't cap
    assert sig.stop < 505.5

    from bot.execution import Position
    pos = Position("trend_day", "X", LONG, 10, ts(11, 0),
                   505.5, sig.stop, None, 1000, sig.stop)
    st.bars_1m.append(Bar("X", ts(12, 0), 509.8, 510.0, 509.5, 510.0, 100))
    st.ind.vwap = 505.0
    strat.manage(pos, st, ts(12, 0))             # +1R+, trail to VWAP
    assert pos.stop_price == 505.0


def test_trend_day_flat_on_unknown_day():
    strat = TrendDay()
    strat.on_session_start()
    st = trend_up_state()
    st.ind.last_close = 502.0                    # ambiguous
    assert strat.on_bar_5m(st, market_with_nifty(+0.5), ts(11, 0)) is None


def test_range_fade_long_at_low():
    strat = RangeFade()
    strat.on_session_start()
    st = range_state()
    sig = strat.on_bar_5m(st, market_with_nifty(0.0), ts(11, 30))
    assert sig is not None and sig.side == LONG
    assert sig.target == st.ind.vwap
    assert sig.stop < st.ind.day_low


def test_range_fade_refuses_trend_day():
    strat = RangeFade()
    strat.on_session_start()
    st = trend_up_state()                        # trend day
    st.ind.rsi7.value = 20.0
    assert strat.on_bar_5m(st, market_with_nifty(+0.5), ts(11, 30)) is None


def test_range_fade_needs_reward_room():
    strat = RangeFade()
    strat.on_session_start()
    st = range_state()
    st.ind.vwap = 499.2                          # target too close to entry
    assert strat.on_bar_5m(st, market_with_nifty(0.0), ts(11, 30)) is None
