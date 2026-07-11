from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from bot.bars import Bar
from bot.clock import IST
from bot.indicators import Ema, Indicators, PrevDayLevels, WilderAtr, WilderRsi
from bot.state import SymbolState


def ts(h, m):
    return datetime(2026, 7, 6, h, m, tzinfo=IST)


def test_ema_seeds_with_sma_then_smooths():
    e = Ema(3)
    assert e.update(1.0) is None
    assert e.update(2.0) is None
    assert e.update(3.0) == pytest.approx(2.0)  # SMA seed
    # k = 2/(3+1) = 0.5 -> 2 + (10-2)*0.5 = 6
    assert e.update(10.0) == pytest.approx(6.0)


def test_wilder_rsi_matches_hand_computation():
    # Classic check: constant gains -> RSI 100
    r = WilderRsi(3)
    for px in [10, 11, 12, 13]:
        val = r.update(px)
    assert val == pytest.approx(100.0)

    # Alternating: hand-computed
    r2 = WilderRsi(2)
    r2.update(10.0)
    r2.update(12.0)   # gain 2
    v = r2.update(11.0)  # loss 1 -> avg_gain=1, avg_loss=0.5 -> RS=2 -> RSI=66.67
    assert v == pytest.approx(100 - 100 / 3, abs=1e-6)


def test_wilder_atr():
    a = WilderAtr(2)
    a.update(Bar("X", ts(9, 15), 100, 102, 98, 101, 0))   # TR = 4 (no prev close)
    v = a.update(Bar("X", ts(9, 16), 101, 103, 100, 102, 0))  # TR = 3
    assert v == pytest.approx(3.5)
    # next: TR = max(2, |104-102|, |102-102|) = 2 -> (3.5*1 + 2)/2 = 2.75
    v = a.update(Bar("X", ts(9, 17), 102, 104, 102, 103, 0))
    assert v == pytest.approx(2.75)


def test_vwap_and_day_state():
    ind = Indicators("X", PrevDayLevels(close=100.0))
    b1 = Bar("X", ts(9, 15), 102, 104, 100, 102, 1000)   # typical 102
    b2 = Bar("X", ts(9, 16), 102, 106, 102, 106, 3000)   # typical ~104.67
    ind.on_bar_1m(b1)
    assert ind.vwap == pytest.approx(102.0)
    ind.on_bar_1m(b2)
    expected = (102 * 1000 + (106 + 102 + 106) / 3 * 3000) / 4000
    assert ind.vwap == pytest.approx(expected)
    assert ind.day_open == 102
    assert ind.day_high == 106
    assert ind.day_low == 100
    assert ind.gap_pct == pytest.approx(2.0)  # 102 open vs 100 prev close


def test_opening_range_completes_after_configured_minutes():
    ind = Indicators("X")
    for i in range(20):
        px = 100 + (i % 3)
        ind.on_bar_1m(Bar("X", ts(9, 15) + timedelta(minutes=i), px, px + 1, px - 1, px, 100))
    assert ind.or_done
    # OR covers 09:15-09:29 bars only: highs max 102+1, lows min 100-1
    assert ind.or_high == 103
    assert ind.or_low == 99


def test_rvol_uses_prev_day_cumulative_profile():
    profile = [1000.0 * (i + 1) for i in range(375)]  # avg 1000/min
    ind = Indicators("X", PrevDayLevels(avg_cum_volume=profile))
    for i in range(10):
        ind.on_bar_1m(Bar("X", ts(9, 15) + timedelta(minutes=i), 100, 100, 100, 100, 2000))
    # cum 20000 vs baseline 10000 -> rvol 2.0
    assert ind.rvol() == pytest.approx(2.0)


def test_vwap_sigma_and_bands_via_symbol_state():
    st = SymbolState("X")
    # 30 one-minute bars oscillating around 100 -> sigma small but positive
    for i in range(30):
        px = 100 + (1 if i % 2 else -1) * 0.5
        st.on_bar_1m(Bar("X", ts(9, 15) + timedelta(minutes=i), px, px + 0.2, px - 0.2, px, 1000))
    assert len(st.bars_5m) >= 5
    assert st.ind.vwap_sigma is not None and st.ind.vwap_sigma > 0
    band = st.ind.vwap_band(2.0)
    assert band is not None
    lo, hi = band
    assert lo < st.ind.vwap < hi
    assert hi - st.ind.vwap == pytest.approx(2.0 * st.ind.vwap_sigma)


def test_symbol_state_rollup_wiring():
    st = SymbolState("X")
    for i in range(6):
        st.on_bar_1m(Bar("X", ts(9, 15) + timedelta(minutes=i), 100, 101, 99, 100, 100))
    assert len(st.bars_5m) == 1
    assert st.bars_5m[0].volume == 500
    st.flush_5m()
    assert len(st.bars_5m) == 2


def test_rsi2_extremes():
    r = WilderRsi(2)
    for px in [100, 99, 98, 97]:
        v = r.update(px)
    assert v == pytest.approx(0.0)
