"""Feature 3 — shared run_and_save runner used by the CLI and the dashboard."""
from __future__ import annotations

from datetime import date

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
