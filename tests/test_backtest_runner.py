"""Feature 3 — shared run_and_save runner used by the CLI and the dashboard."""
from __future__ import annotations

from datetime import date

import pytest

from bot import backtest, history
from bot.backtest import PERIODS, BacktestResult, _seed_strategies, run_and_save, summarize
from bot.discovery.mixer import SEED_GENES


def test_periods_map_to_sessions_and_depth():
    for label, (sessions, depth) in PERIODS.items():
        assert sessions > 0 and depth >= sessions


def test_seed_strategies_builds_the_seed_library():
    strats = _seed_strategies()
    assert [s.name for s in strats] == ["DISCOVERED_EQ"]
    assert len(strats[0].specs) == len(SEED_GENES["DISCOVERED_EQ"])
    assert all(s.source == "manual" for s in strats[0].specs)


def test_summarize_computes_metrics():
    res = BacktestResult()
    res.sessions = [date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)]
    res.daily_pnl = {res.sessions[0]: 1000.0, res.sessions[1]: -400.0,
                     res.sessions[2]: 600.0}
    res.equity_curve = [(res.sessions[0], 101000.0), (res.sessions[1], 100600.0),
                        (res.sessions[2], 101200.0)]
    s = summarize(res, starting_cash=100000.0)
    assert s["sessions"] == 3
    assert s["total_net"] == 1200.0
    assert s["green_days"] == 2
    assert s["final_equity"] == 101200.0


def test_run_and_save_reports_missing_bars(mem_db):
    _, summary = run_and_save(period="1 week", max_instruments=3)
    assert summary.get("error")


# --- Fyers-only backtest source --------------------------------------------

def test_backtest_fetch_defaults_to_fyers(monkeypatch):
    """The backtest fetch path uses Fyers /history by default — never yfinance."""
    calls: dict[str, int] = {"fyers": 0, "yf": 0, "dhan": 0}
    monkeypatch.setattr(history, "fetch_1m_fyers",
                        lambda syms, s, e: calls.__setitem__("fyers", calls["fyers"] + 1) or 7)
    monkeypatch.setattr(history, "fetch_1m_yfinance",
                        lambda syms, s, e: calls.__setitem__("yf", calls["yf"] + 1) or 0)
    n = backtest._fetch(["TCS"], date(2026, 6, 1), date(2026, 6, 10), "fyers")
    assert n == 7
    assert calls == {"fyers": 1, "yf": 0, "dhan": 0}


def test_fetch_1m_fyers_fails_loud_without_token(monkeypatch):
    """No Fyers token → FyersHistoryUnavailable, NOT a silent 0 / yfinance fallback."""
    import bot.fyers_auth as fyers_auth
    monkeypatch.setattr(fyers_auth, "ensure_access_token", lambda: None)
    with pytest.raises(history.FyersHistoryUnavailable):
        history.fetch_1m_fyers(["TCS"], date(2026, 6, 1), date(2026, 6, 10))
