"""Day-type classification: decide by late morning whether today is a trend
day (deploy trend-following) or a range day (deploy fades) — and stay flat on
ambiguous days. Thresholds in config.DAYTYPE. Pure logic, unit-testable."""
from __future__ import annotations

import config
from bot.state import MarketState, SymbolState

TREND_UP = "TREND_UP"
TREND_DOWN = "TREND_DOWN"
RANGE = "RANGE"
UNKNOWN = "UNKNOWN"


def classify(st: SymbolState, market: MarketState) -> str:
    ind = st.ind
    p = config.DAYTYPE
    if (ind.day_high is None or ind.day_low is None or ind.vwap is None
            or ind.day_change_pct is None or ind.last_close is None):
        return UNKNOWN
    rng = ind.day_high - ind.day_low
    if rng <= 0:
        return UNKNOWN
    pos_in_range = (ind.last_close - ind.day_low) / rng

    nifty = market.indices.get("NIFTY")
    nifty_move = nifty.move_pct_from_open() if nifty else None
    slope_up = ind.vwap_slope_up(p["vwap_slope_bars"])

    # Trend day: decisive move, closing near the extreme, on the right side of
    # a sloping VWAP, with the index agreeing.
    if (ind.day_change_pct >= p["trend_min_change_pct"]
            and pos_in_range >= p["trend_range_pos"]
            and ind.last_close > ind.vwap and slope_up is True
            and (nifty_move is None or nifty_move >= p["trend_nifty_min_pct"])):
        return TREND_UP
    if (ind.day_change_pct <= -p["trend_min_change_pct"]
            and pos_in_range <= 1 - p["trend_range_pos"]
            and ind.last_close < ind.vwap and slope_up is False
            and (nifty_move is None or nifty_move <= -p["trend_nifty_min_pct"])):
        return TREND_DOWN

    # Range day: compressed range, flat on the day, price has spent real time
    # on BOTH sides of VWAP.
    avg_range = ind.prev_day.avg_daily_range_pct
    if (avg_range and ind.day_range_pct is not None
            and ind.day_range_pct <= p["range_max_vs_avg"] * avg_range
            and abs(ind.day_change_pct) <= p["range_max_change_pct"]
            and min(ind.minutes_above_vwap, ind.minutes_below_vwap)
            >= p["range_min_side_minutes"]):
        return RANGE
    return UNKNOWN
