from __future__ import annotations

import pytest

from bot import costs


def test_brokerage_capped_at_20():
    # 0.03% of 1,00,000 = 30 -> capped at 20
    assert costs.order_brokerage(100_000) == 20.0
    # 0.03% of 10,000 = 3 -> under cap
    assert costs.order_brokerage(10_000) == pytest.approx(3.0)


def test_intraday_costs_hand_computed():
    # Round trip: buy 100 sh @ 500 = 50,000 ; sell @ 505 = 50,500
    c = costs.intraday_costs(50_000, 50_500)
    brokerage = 15.0 + 15.15                    # 0.03% each side, under cap
    stt = 50_500 * 0.00025                      # 12.625, sell only
    exch = 100_500 * 0.0000297                  # 2.98485
    sebi = 100_500 * 0.000001                   # 0.1005
    stamp = 50_000 * 0.00003                    # 1.5, buy only
    gst = (brokerage + exch + sebi) * 0.18
    total = brokerage + stt + exch + sebi + stamp + gst
    assert c["brokerage"] == pytest.approx(brokerage, abs=0.01)
    assert c["stt"] == pytest.approx(stt, abs=0.01)
    assert c["stamp"] == pytest.approx(1.5, abs=0.01)
    assert c["total"] == pytest.approx(total, abs=0.05)


def test_slippage_direction():
    assert costs.slippage_price(1000.0, side_is_buy=True) > 1000.0
    assert costs.slippage_price(1000.0, side_is_buy=False) < 1000.0
