from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import db  # noqa: E402
from bot.bars import Bar  # noqa: E402
from bot.clock import IST  # noqa: E402


@pytest.fixture()
def mem_db():
    db.set_db_path(":memory:")
    conn = db.connect()
    yield conn
    db.set_db_path(None)


def make_bar(symbol: str = "TEST", ts: datetime | None = None, o: float = 100.0,
             h: float | None = None, l: float | None = None, c: float = 100.0,  # noqa: E741
             v: int = 1000, interval: int = 1) -> Bar:
    ts = ts or datetime(2026, 7, 6, 9, 15, tzinfo=IST)
    return Bar(
        symbol=symbol, ts=ts, open=o,
        high=h if h is not None else max(o, c),
        low=l if l is not None else min(o, c),
        close=c, volume=v, interval=interval,
    )


def bar_series(symbol: str, start: datetime, closes: list[float],
               volumes: list[int] | None = None, interval: int = 1) -> list[Bar]:
    """Build consecutive bars where each bar opens at the prior close."""
    bars = []
    prev_close = closes[0]
    for i, c in enumerate(closes):
        o = prev_close
        bars.append(make_bar(
            symbol=symbol, ts=start + timedelta(minutes=i * interval),
            o=o, h=max(o, c) * 1.0005, l=min(o, c) * 0.9995, c=c,
            v=(volumes[i] if volumes else 1000), interval=interval,
        ))
        prev_close = c
    return bars
