"""Incremental intraday indicators, updated bar-by-bar. Pure logic, no I/O.

One Indicators object per symbol per session. 1m bars drive VWAP/day state;
5m bars drive RSI/EMA/ATR/sigma bands and the opening range.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import config
from bot import clock
from bot.bars import Bar


class Ema:
    def __init__(self, period: int):
        self.period = period
        self.value: float | None = None
        self._k = 2.0 / (period + 1)
        self._seed: list[float] = []

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) >= self.period:
                self.value = sum(self._seed) / len(self._seed)
        else:
            self.value += (x - self.value) * self._k
        return self.value


class WilderRsi:
    def __init__(self, period: int):
        self.period = period
        self.value: float | None = None
        self._prev: float | None = None
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._gains: list[float] = []
        self._losses: list[float] = []

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self._avg_gain is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None
            self._avg_gain = sum(self._gains) / self.period
            self._avg_loss = sum(self._losses) / self.period
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
        if self._avg_loss == 0:
            self.value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class WilderAtr:
    def __init__(self, period: int = 14):
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._seed: list[float] = []

    def update(self, bar: Bar) -> float | None:
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(bar.high - bar.low,
                     abs(bar.high - self._prev_close),
                     abs(bar.low - self._prev_close))
        self._prev_close = bar.close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) >= self.period:
                self.value = sum(self._seed) / self.period
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


def atr_stop_floor(entry: float | None, stop: float | None, atr: float | None,
                   side: str, *, min_stop_atr_mult: float,
                   max_risk_pct: float) -> float | None:
    """Widen a too-tight stop to at least `min_stop_atr_mult` x ATR, so a
    support/structure level sitting just under price can't produce a stop inside
    per-bar noise. Always clamped to `max_risk_pct` so the floor can never widen
    risk past the ceiling. Only ever widens — a stop already beyond the floor is
    returned unchanged.

    No-ops (returns `stop` untouched) when ATR is unavailable (None/0 — too
    little history), the multiple/ceiling is disabled (<=0), or entry/stop are
    missing. `side` is LONG or SHORT (a short's stop sits above entry).
    """
    if not entry or entry <= 0 or stop is None:
        return stop
    if not atr or atr <= 0 or min_stop_atr_mult <= 0 or max_risk_pct <= 0:
        return stop
    risk_pct = abs(entry - stop) / entry * 100.0
    atr_pct = atr / entry * 100.0
    min_stop_pct = min(min_stop_atr_mult * atr_pct, max_risk_pct)
    if risk_pct >= min_stop_pct:
        return stop
    return (entry * (1 - min_stop_pct / 100.0) if side == "LONG"
            else entry * (1 + min_stop_pct / 100.0))


@dataclass
class PrevDayLevels:
    """Reference levels computed from history during warmup."""
    high: float | None = None
    low: float | None = None
    close: float | None = None
    avg_daily_range_pct: float | None = None      # mean (H-L)/C over lookback
    # average cumulative volume at each minute-of-session index (for RVOL)
    avg_cum_volume: list[float] = field(default_factory=list)
    avg_1m_turnover: float | None = None          # rupees per minute


class Indicators:
    """Per-symbol session state. Call on_bar_1m / on_bar_5m with completed bars."""

    def __init__(self, symbol: str, prev_day: PrevDayLevels | None = None):
        self.symbol = symbol
        self.prev_day = prev_day or PrevDayLevels()

        # session day state (from 1m bars)
        self.day_open: float | None = None
        self.day_high: float | None = None
        self.day_low: float | None = None
        self.last_close: float | None = None
        self.cum_volume: int = 0
        self._cum_pv: float = 0.0
        self.vwap: float | None = None
        self.minutes_above_vwap: int = 0
        self.minutes_below_vwap: int = 0
        self.session_minutes: int = 0

        # opening range (first N session minutes, from 1m bars)
        self.or_high: float | None = None
        self.or_low: float | None = None
        self.or_volume: int = 0
        self.or_done: bool = False

        # 5m-driven (or whatever the strategy interval is)
        self.ema20 = Ema(20)
        self.rsi14 = WilderRsi(14)
        self.rsi7 = WilderRsi(7)      # fast enough to be ready on 15m bars
        self.rsi2 = WilderRsi(2)
        self.atr14 = WilderAtr(14)
        self._vwap_dev_sq: list[float] = []       # (close-vwap)^2 per 5m bar
        self.vwap_sigma: float | None = None
        self._vwap_track: list[float] = []        # vwap value per 5m bar (slope)

    # -- derived ------------------------------------------------------------

    @property
    def gap_pct(self) -> float | None:
        if self.day_open is None or not self.prev_day.close:
            return None
        return (self.day_open - self.prev_day.close) / self.prev_day.close * 100.0

    @property
    def day_change_pct(self) -> float | None:
        if self.last_close is None or self.day_open in (None, 0):
            return None
        return (self.last_close - self.day_open) / self.day_open * 100.0

    @property
    def day_range_pct(self) -> float | None:
        if self.day_high is None or self.day_low is None or not self.last_close:
            return None
        return (self.day_high - self.day_low) / self.last_close * 100.0

    def rvol(self) -> float | None:
        """Cumulative session volume vs the historical average at this minute."""
        profile = self.prev_day.avg_cum_volume
        idx = min(self.session_minutes, len(profile)) - 1
        if idx < 0 or not profile:
            return None
        baseline = profile[idx]
        if baseline <= 0:
            return None
        return self.cum_volume / baseline

    def vwap_slope_up(self, bars: int) -> bool | None:
        if len(self._vwap_track) < bars + 1:
            return None
        return self._vwap_track[-1] > self._vwap_track[-1 - bars]

    def vwap_band(self, sigmas: float) -> tuple[float, float] | None:
        if self.vwap is None or self.vwap_sigma is None or self.vwap_sigma <= 0:
            return None
        return (self.vwap - sigmas * self.vwap_sigma, self.vwap + sigmas * self.vwap_sigma)

    # -- updates ------------------------------------------------------------

    def on_bar_1m(self, bar: Bar) -> None:
        if self.day_open is None:
            self.day_open = bar.open
            self.day_high, self.day_low = bar.high, bar.low
        else:
            self.day_high = max(self.day_high, bar.high)
            self.day_low = min(self.day_low, bar.low)
        self.last_close = bar.close
        self.session_minutes += 1

        self.cum_volume += bar.volume
        self._cum_pv += bar.typical * bar.volume
        if self.cum_volume > 0:
            self.vwap = self._cum_pv / self.cum_volume
        if self.vwap is not None:
            if bar.close >= self.vwap:
                self.minutes_above_vwap += 1
            else:
                self.minutes_below_vwap += 1

        or_minutes = config.STRATEGY_PARAMS["orb"]["or_minutes"]
        if not self.or_done:
            minute_in_session = clock.minutes_since_open(bar.ts)
            if minute_in_session < or_minutes:
                self.or_high = bar.high if self.or_high is None else max(self.or_high, bar.high)
                self.or_low = bar.low if self.or_low is None else min(self.or_low, bar.low)
                self.or_volume += bar.volume
            if minute_in_session >= or_minutes - 1:
                self.or_done = True

    def on_bar_5m(self, bar: Bar) -> None:
        self.ema20.update(bar.close)
        self.rsi14.update(bar.close)
        self.rsi7.update(bar.close)
        self.rsi2.update(bar.close)
        self.atr14.update(bar)
        if self.vwap is not None:
            self._vwap_track.append(self.vwap)
            self._vwap_dev_sq.append((bar.close - self.vwap) ** 2)
            if len(self._vwap_dev_sq) >= 3:
                self.vwap_sigma = math.sqrt(
                    sum(self._vwap_dev_sq) / len(self._vwap_dev_sq)
                )
