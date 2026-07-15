"""The Streamlit dashboard must render both sidebar views without exceptions.

Uses streamlit.testing.v1.AppTest against a small seeded temp DB (not the real
985 MB data/bot.db) so the test is fast and hermetic.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import config
from bot import db
from bot.clock import IST

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "dash.db"
    db.set_db_path(dbfile)
    db.connect()  # runs migrations, creates the file

    ts = datetime(2026, 7, 6, 9, 30, tzinfo=IST).isoformat()
    xt = datetime(2026, 7, 6, 10, 30, tzinfo=IST).isoformat()
    run_id = db.start_run("PAPER", "2026-07-06", "fyers-ws", ts)
    db.record_trade(run_id=run_id, mode="PAPER", strategy="orb", symbol="RELIANCE",
                    side="LONG", qty=10, entry_ts=ts, entry_price=100.0,
                    exit_ts=xt, exit_price=102.0, gross_pnl=200.0, costs=20.0,
                    net_pnl=180.0, r_multiple=1.5, planned_stop=99.0,
                    planned_target=103.0, exit_reason="TARGET", feed_source="fyers-ws")
    db.open_position(run_id=run_id, mode="PAPER", strategy="gap", symbol="SBIN",
                     side="LONG", qty=5, entry_ts=ts, entry_price=500.0,
                     stop_price=495.0, target_price=510.0, margin_used=500.0)
    db.upsert_bars([("SBIN", xt, 500.0, 505.0, 499.0, 504.0, 1000, "fyers")])
    db.log_equity("PAPER", xt, 2_000_180.0, 1_999_680.0, 500.0, 1, 180.0)
    db.kv_set("engine_heartbeat", json.dumps({
        "mode": "PAPER", "phase": "OPEN", "feed": "fyers-ws", "equity": 2_000_180,
        "day_pnl": 180, "entries_today": 1, "entries_budget": 200,
        "trades_today": 1, "halted": False, "strategies": ["orb", "gap"],
        "benched": [], "wall_ts": xt,
    }))

    db.set_db_path(None)
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    yield dbfile


def _fresh_app():
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    st.cache_resource.clear()
    return AppTest.from_file(str(ROOT / "dashboard_web.py"), default_timeout=90)


def test_paper_view_renders(seeded_db):
    at = _fresh_app().run()
    assert not at.exception
    # default view is Paper — its tab labels should be present
    labels = [t.label for t in at.tabs]
    assert "📊 Summary" in labels and "📖 Open Book" in labels


def test_live_view_renders(seeded_db):
    at = _fresh_app().run()
    assert not at.exception
    at.sidebar.radio[0].set_value("🟢 Live").run()
    assert not at.exception
    labels = [t.label for t in at.tabs]
    assert "Readiness" in labels and "Broker & Gate" in labels
