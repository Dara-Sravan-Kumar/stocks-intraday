"""Indian equity intraday (MIS) transaction cost model. Pure functions.

Rates live in config.COSTS (verified against Dhan pricing 2026-07-09).
"""
from __future__ import annotations

import config


def order_brokerage(order_value: float) -> float:
    c = config.COSTS
    return min(order_value * c["brokerage_pct"] / 100.0, c["brokerage_cap"])


def intraday_costs(buy_value: float, sell_value: float) -> dict[str, float]:
    """Full round-trip cost breakdown for an intraday trade.

    buy_value / sell_value are qty * price for each leg.
    """
    c = config.COSTS
    brokerage = order_brokerage(buy_value) + order_brokerage(sell_value)
    stt = sell_value * c["stt_sell_pct"] / 100.0
    exch = (buy_value + sell_value) * c["exchange_txn_pct"] / 100.0
    sebi = (buy_value + sell_value) * c["sebi_pct"] / 100.0
    stamp = buy_value * c["stamp_buy_pct"] / 100.0
    gst = (brokerage + exch + sebi) * c["gst_pct"] / 100.0
    total = brokerage + stt + exch + sebi + stamp + gst
    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange": round(exch, 2),
        "sebi": round(sebi, 4),
        "stamp": round(stamp, 2),
        "gst": round(gst, 2),
        "total": round(total, 2),
    }


def options_costs(buy_value: float, sell_value: float) -> dict[str, float]:
    """Round-trip cost breakdown for index options (values = premium * qty)."""
    c = config.OPTION_COSTS
    brokerage = c["brokerage_flat"] * 2
    stt = sell_value * c["stt_sell_pct"] / 100.0
    exch = (buy_value + sell_value) * c["exchange_txn_pct"] / 100.0
    sebi = (buy_value + sell_value) * c["sebi_pct"] / 100.0
    stamp = buy_value * c["stamp_buy_pct"] / 100.0
    gst = (brokerage + exch + sebi) * c["gst_pct"] / 100.0
    total = brokerage + stt + exch + sebi + stamp + gst
    return {
        "brokerage": round(brokerage, 2), "stt": round(stt, 2),
        "exchange": round(exch, 2), "sebi": round(sebi, 4),
        "stamp": round(stamp, 2), "gst": round(gst, 2),
        "total": round(total, 2),
    }


def slippage_price(price: float, side_is_buy: bool,
                   instrument: str = "EQ") -> float:
    """Worse-by-slippage fill price for paper trades."""
    if instrument == "OPT":
        slip = price * config.OPTION_SLIPPAGE_PCT / 100.0
    else:
        slip = price * config.SLIPPAGE_BPS / 10_000.0
    return price + slip if side_is_buy else price - slip
