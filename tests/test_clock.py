from __future__ import annotations

from datetime import date, datetime

from bot import clock
from bot.clock import IST


def dt(h, m, s=0, d=6):
    # 2026-07-06 is a Monday (trading day)
    return datetime(2026, 7, d, h, m, s, tzinfo=IST)


def test_trading_day_weekday():
    assert clock.is_trading_day(date(2026, 7, 6))       # Monday
    assert not clock.is_trading_day(date(2026, 7, 4))   # Saturday
    assert not clock.is_trading_day(date(2026, 7, 5))   # Sunday


def test_trading_day_holiday():
    assert not clock.is_trading_day(date(2026, 1, 26))  # Republic Day
    assert not clock.is_trading_day(date(2026, 12, 25))  # Christmas


def test_phase_boundaries():
    assert clock.phase(dt(8, 59, 59)) == clock.CLOSED
    assert clock.phase(dt(9, 0)) == clock.PREOPEN
    assert clock.phase(dt(9, 14, 59)) == clock.PREOPEN
    assert clock.phase(dt(9, 15)) == clock.OPEN
    assert clock.phase(dt(14, 44, 59)) == clock.OPEN
    assert clock.phase(dt(14, 45)) == clock.NO_NEW
    assert clock.phase(dt(15, 11, 59)) == clock.NO_NEW
    assert clock.phase(dt(15, 12)) == clock.SQUAREOFF
    assert clock.phase(dt(15, 29, 59)) == clock.SQUAREOFF
    assert clock.phase(dt(15, 30)) == clock.CLOSED


def test_phase_on_weekend_and_holiday():
    sat = datetime(2026, 7, 4, 10, 0, tzinfo=IST)
    assert clock.phase(sat) == clock.CLOSED
    holiday = datetime(2026, 1, 26, 10, 0, tzinfo=IST)
    assert clock.phase(holiday) == clock.CLOSED


def test_entries_allowed_buffer():
    assert not clock.entries_allowed(dt(9, 15))   # open but before entries_start
    assert not clock.entries_allowed(dt(9, 19, 59))
    assert clock.entries_allowed(dt(9, 20))
    assert clock.entries_allowed(dt(14, 44))
    assert not clock.entries_allowed(dt(14, 45))  # NO_NEW


def test_prev_next_trading_day_skips_weekend_and_holiday():
    assert clock.prev_trading_day(date(2026, 7, 6)) == date(2026, 7, 3)
    assert clock.next_trading_day(date(2026, 7, 3)) == date(2026, 7, 6)
    # Republic Day Mon 2026-01-26: Friday before is 01-23, next is Tue 01-27
    assert clock.next_trading_day(date(2026, 1, 23)) == date(2026, 1, 27)
    assert clock.prev_trading_day(date(2026, 1, 27)) == date(2026, 1, 23)


def test_minutes_since_open():
    assert clock.minutes_since_open(dt(9, 15)) == 0
    assert clock.minutes_since_open(dt(9, 45)) == 30
    assert clock.minutes_since_open(dt(10, 15)) == 60
