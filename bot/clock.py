"""NSE session clock: IST time, trading days, session phases.

All time logic lives here. The engine injects `now_fn` so replay/backtest can
drive time from bar timestamps instead of the wall clock.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import config

IST = ZoneInfo(config.TIMEZONE)

# Session phases in chronological order.
CLOSED = "CLOSED"          # outside session / holiday / weekend
PREOPEN = "PREOPEN"        # 09:00 - 09:15 on a trading day
OPEN = "OPEN"              # 09:15 - no_new_entries: full trading
NO_NEW = "NO_NEW"          # manage/exit only, no fresh entries
SQUAREOFF = "SQUAREOFF"    # force-exit window until close

_HOLIDAYS = {date.fromisoformat(d) for d in config.NSE_HOLIDAYS}


def _t(key: str) -> time:
    h, m = config.SESSION[key].split(":")
    return time(int(h), int(m))


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _HOLIDAYS


def phase(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    now = now.astimezone(IST)
    if not is_trading_day(now.date()):
        return CLOSED
    t = now.time()
    if t < _t("preopen_start") or t >= _t("market_close"):
        return CLOSED
    if t < _t("market_open"):
        return PREOPEN
    if t < _t("no_new_entries"):
        return OPEN
    if t < _t("square_off"):
        return NO_NEW
    return SQUAREOFF


def entries_allowed(now: datetime) -> bool:
    """OPEN phase, but also past the entries_start settle-in buffer."""
    if phase(now) != OPEN:
        return False
    return now.astimezone(IST).time() >= _t("entries_start")


def session_open_dt(d: date) -> datetime:
    return datetime.combine(d, _t("market_open"), tzinfo=IST)


def session_close_dt(d: date) -> datetime:
    return datetime.combine(d, _t("market_close"), tzinfo=IST)


def square_off_dt(d: date) -> datetime:
    return datetime.combine(d, _t("square_off"), tzinfo=IST)


def prev_trading_day(d: date) -> date:
    cur = d - timedelta(days=1)
    while not is_trading_day(cur):
        cur -= timedelta(days=1)
    return cur


def next_trading_day(d: date) -> date:
    cur = d + timedelta(days=1)
    while not is_trading_day(cur):
        cur += timedelta(days=1)
    return cur


def minutes_since_open(now: datetime) -> float:
    now = now.astimezone(IST)
    open_dt = session_open_dt(now.date())
    return (now - open_dt).total_seconds() / 60.0


def parse_hhmm(value: str, on_date: date) -> datetime:
    """Turn a config 'HH:MM' string into an aware IST datetime on the given date."""
    h, m = value.split(":")
    return datetime.combine(on_date, time(int(h), int(m)), tzinfo=IST)
