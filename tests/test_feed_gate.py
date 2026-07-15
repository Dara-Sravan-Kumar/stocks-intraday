"""Degraded-feed booking gate (FIX 3).

Trades booked while the feed is the yfinance fallback (an intentional --feed yf
run OR a mid-session degrade from Fyers) must be tagged and EXCLUDED from the
promotion track record. A Fyers feed that cannot fall back (options/live) must
abort rather than silently degrade.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import config
from bot import db, reports
from bot.clock import IST
from bot.feeds.fyers_feed import FyersFeed


def _record(strategy: str, net: float, feed_source: str | None) -> None:
    ts = datetime.now(tz=IST).isoformat()
    db.record_trade(
        run_id=None, mode="PAPER", strategy=strategy, symbol="RELIANCE",
        side="LONG", qty=10, entry_ts=ts, entry_price=100.0,
        exit_ts=ts, exit_price=101.0, gross_pnl=net, costs=0.0, net_pnl=net,
        r_multiple=1.0, planned_stop=99.0, planned_target=102.0,
        exit_reason="TARGET", feed_source=feed_source,
    )


def test_reports_helper_flags_fallback_sources():
    assert reports._is_fallback_feed("yfinance") is True
    assert reports._is_fallback_feed("yfinance (degraded from fyers)") is True
    assert reports._is_fallback_feed("fyers-ws") is False
    assert reports._is_fallback_feed(None) is False       # legacy trades stay in
    assert reports._is_fallback_feed("") is False


def test_fallback_trades_excluded_from_promotion(mem_db):
    # same strategy, mixed provenance: only the fyers-ws trades should count
    for _ in range(3):
        _record("orb", 500.0, "fyers-ws")
    for _ in range(2):
        _record("orb", 500.0, "yfinance (degraded from fyers)")
    # a strategy whose ONLY trades are on the fallback must vanish entirely
    for _ in range(4):
        _record("degraded_only", 500.0, "yfinance")

    results = reports.promotion_readiness(mode="PAPER")
    by_name = {r["strategy"]: r for r in results}

    assert "orb" in by_name
    assert by_name["orb"]["trades"] == 3            # 2 yfinance trades excluded
    assert "degraded_only" not in by_name           # fully excluded


def test_fyers_feed_aborts_when_fallback_disabled(monkeypatch):
    """allow_degrade=False (options/live): a feed failure aborts instead of
    switching to yfinance, and the engine sees the feed as exhausted."""
    monkeypatch.setattr("bot.alerts.send", lambda msg: True)
    feed = FyersFeed(["RELIANCE"], allow_degrade=False)
    assert feed.exhausted is False
    feed._degrade("websocket dead")
    assert feed._fallback is None                   # did NOT fall back
    assert feed.exhausted is True                   # engine loop will stop
    assert "aborted" in feed.source_name


def test_fyers_feed_degrades_when_allowed(monkeypatch):
    monkeypatch.setattr("bot.alerts.send", lambda msg: True)
    feed = FyersFeed(["RELIANCE"], allow_degrade=True)
    feed._degrade("websocket dead")
    assert feed._fallback is not None
    assert feed.exhausted is False
    assert "yfinance" in feed.source_name and "degraded" in feed.source_name


# ---------------------------------------------------------------------------
# Fyers-ONLY booking gate (freeze on fallback) — the "don't book" policy.
# The paper book may only be opened/closed/marked on the real Fyers feed.
# ---------------------------------------------------------------------------
from bot.engine import Engine  # noqa: E402
from bot.execution import LONG  # noqa: E402
from bot.execution.paper_broker import PaperBroker  # noqa: E402
from bot.risk import DayState, RiskEngine  # noqa: E402
from bot.state import MarketState  # noqa: E402


class _StubFeed:
    """Minimal Feed whose source_name is whatever we want to test the gate with."""
    def __init__(self, source: str):
        self._source = source

    def start(self):  # pragma: no cover - trivial
        pass

    def stop(self):  # pragma: no cover - trivial
        pass

    def poll(self):  # pragma: no cover - trivial
        return []

    @property
    def exhausted(self) -> bool:
        return False

    @property
    def source_name(self) -> str:
        return self._source


def _make_engine(source: str, require: bool):
    return Engine(
        mode="PAPER", feed=_StubFeed(source), broker=PaperBroker(100_000.0),
        strategies=[], risk=RiskEngine(), market=MarketState([], {}),
        persist=True, require_fyers_feed=require,
    )


def test_book_frozen_matrix():
    # require_fyers_feed=False (backtest / --feed yf dev / replay): never frozen.
    for src in ("fyers-ws", "yfinance", "yfinance (degraded from fyers)", "replay"):
        assert _make_engine(src, require=False)._book_frozen() is False

    # require_fyers_feed=True: only the real Fyers websocket may touch the book.
    assert _make_engine("fyers-ws", require=True)._book_frozen() is False
    assert _make_engine("yfinance", require=True)._book_frozen() is True
    assert _make_engine("yfinance (degraded from fyers)",
                        require=True)._book_frozen() is True
    assert _make_engine("fyers-ws (aborted: feed failure)",
                        require=True)._book_frozen() is True
    assert _make_engine("dhan-ws", require=True)._book_frozen() is True


def test_frozen_feed_does_not_open_close_or_mark(mem_db, monkeypatch):
    """On a degraded (fallback) feed with require_fyers_feed=True, opening,
    closing, and marking are all blocked; a freeze alert fires once."""
    sent: list[str] = []
    monkeypatch.setattr("bot.engine.alerts.send", lambda msg: sent.append(msg) or True)

    eng = _make_engine("yfinance (degraded from fyers)", require=True)
    eng.run_id = db.start_run("PAPER", "2026-07-06", eng.feed.source_name, "t")
    eng.now = datetime(2026, 7, 6, 10, 0, tzinfo=IST)
    eng.day = DayState(start_equity=100_000.0)

    # open a position directly, then try to close it through the engine
    ts = datetime(2026, 7, 6, 9, 30, tzinfo=IST)
    pos = eng.broker.open_position("orb", "RELIANCE", LONG, 10, 100.0, ts,
                                   stop=99.0, target=102.0, margin=200.0)
    eng._close(pos, 102.0, eng.now, "TARGET")
    assert pos in eng.broker.positions                 # NOT closed
    assert db.trades_for("PAPER") == []                # no trade recorded

    eng._mark_equity()
    marked = db.connect().execute("SELECT COUNT(*) AS n FROM equity_log").fetchone()
    assert marked["n"] == 0                            # book not marked
    assert sent and "FROZEN" in sent[0]                # single clear alert


def test_real_fyers_feed_books_normally(mem_db):
    """The same close on the real Fyers feed goes through — the gate is specific
    to the fallback, not a blanket freeze."""
    eng = _make_engine("fyers-ws", require=True)
    eng.run_id = db.start_run("PAPER", "2026-07-06", "fyers-ws", "t")
    eng.now = datetime(2026, 7, 6, 10, 0, tzinfo=IST)
    eng.day = DayState(start_equity=100_000.0)

    ts = datetime(2026, 7, 6, 9, 30, tzinfo=IST)
    pos = eng.broker.open_position("orb", "RELIANCE", LONG, 10, 100.0, ts,
                                   stop=99.0, target=102.0, margin=200.0)
    eng._close(pos, 102.0, eng.now, "TARGET")
    assert pos not in eng.broker.positions             # closed
    trades = db.trades_for("PAPER")
    assert len(trades) == 1 and trades[0]["feed_source"] == "fyers-ws"


def test_frozen_book_reminder_repeats_hourly(mem_db, monkeypatch):
    """While the book stays frozen, the Discord login-reminder fires on entry and
    again after >= 60 min — but NOT within the hour — and re-arms on recovery."""
    from bot.bars import Bar

    sent: list[str] = []
    monkeypatch.setattr("bot.engine.alerts.send", lambda m: sent.append(m) or True)

    eng = _make_engine("yfinance (degraded from fyers)", require=True)
    eng.persist = False                                # no DB writes needed here
    eng.day = DayState(start_equity=100_000.0)
    base = datetime(2026, 7, 6, 9, 20, tzinfo=IST)

    def drive(offset_min: int) -> None:
        # process_minute sets self.now = max(bar.ts) + 1 min, so now = base+offset.
        ts = base + timedelta(minutes=offset_min - 1)
        eng.process_minute([Bar(symbol="RELIANCE", ts=ts, open=100.0, high=100.0,
                                low=100.0, close=100.0, volume=1000, interval=1)])

    drive(0)                                           # enter frozen
    assert len(sent) == 1
    assert "FROZEN" in sent[0] and "login" in sent[0].lower()
    drive(30)
    assert len(sent) == 1                              # within the hour: silent
    drive(60)
    assert len(sent) == 2                              # >= 60 min: hourly repeat
    drive(75)
    assert len(sent) == 2                              # only 15 min since last

    eng.feed._source = "fyers-ws"                      # feed recovers → timer resets
    drive(80)
    assert len(sent) == 2                              # not frozen: no alert
    eng.feed._source = "yfinance (degraded from fyers)"
    drive(85)
    assert len(sent) == 3                              # re-armed → prompt re-alert
