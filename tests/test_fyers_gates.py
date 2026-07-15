"""Fyers live gates must independently block, and mirroring must respect the
allowlist — same contract as the Dhan path."""
from __future__ import annotations

from datetime import datetime

import pytest

import config
from bot import fyers_auth
from bot.clock import IST
from bot.execution import LONG
from bot.execution.dhan_broker import LiveTradingBlocked
from bot.execution.fyers_broker import FyersHybridBroker, check_live_gates
from bot.execution.paper_broker import PaperBroker


class FakeFyersClient:
    def __init__(self):
        self.orders = []

    def place_order(self, data):
        self.orders.append(data)
        return {"s": "ok", "id": f"o{len(self.orders)}"}


def all_gates_open(monkeypatch):
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(config, "LIVE_STRATEGY_ALLOWLIST", {"vwap_pullback"})
    monkeypatch.setenv("LIVE_CONFIRM", config.LIVE_CONFIRM_STRING)
    monkeypatch.setenv("FYERS_APP_ID", "AB12345-100")
    monkeypatch.setenv("FYERS_SECRET_ID", "secret")


def ts():
    return datetime(2026, 7, 6, 10, 0, tzinfo=IST)


def test_each_gate_blocks(monkeypatch):
    all_gates_open(monkeypatch)
    check_live_gates()  # baseline: passes

    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", False)
    with pytest.raises(LiveTradingBlocked, match="gate 1"):
        check_live_gates()
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", True)

    monkeypatch.setenv("LIVE_CONFIRM", "yes")
    with pytest.raises(LiveTradingBlocked, match="gate 2"):
        check_live_gates()
    monkeypatch.setenv("LIVE_CONFIRM", config.LIVE_CONFIRM_STRING)

    monkeypatch.setenv("FYERS_APP_ID", "")
    with pytest.raises(LiveTradingBlocked, match="gate 3"):
        check_live_gates()
    monkeypatch.setenv("FYERS_APP_ID", "AB12345-100")

    monkeypatch.setattr(config, "LIVE_STRATEGY_ALLOWLIST", set())
    with pytest.raises(LiveTradingBlocked, match="gate 5"):
        check_live_gates()


def test_allowlist_controls_mirroring(monkeypatch, mem_db):
    all_gates_open(monkeypatch)
    client = FakeFyersClient()
    broker = FyersHybridBroker(PaperBroker(100_000), client=client)

    pos = broker.open_position("vwap_pullback", "RELIANCE", LONG, 100, 500.0,
                               ts(), stop=495.0, target=510.0)
    assert len(client.orders) == 1
    order = client.orders[0]
    assert order["symbol"] == "NSE:RELIANCE-EQ"
    assert order["side"] == 1 and order["type"] == 2
    assert order["productType"] == "INTRADAY"
    assert pos.scratch["live_qty"] == order["qty"] > 0

    trade = broker.close_position(pos, 510.0, ts(), "TARGET")
    assert trade.net_pnl != 0
    assert len(client.orders) == 2 and client.orders[1]["side"] == -1

    # non-allowlisted strategy: paper only
    pos2 = broker.open_position("orb", "SBIN", LONG, 100, 500.0, ts(), 495.0, 510.0)
    assert len(client.orders) == 2
    assert "live_qty" not in pos2.scratch


def test_fyers_auth_token_freshness(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "FYERS_TOKENS_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    # no tokens at all -> None
    assert fyers_auth.ensure_access_token() is None
    # token saved today -> returned without refresh
    fyers_auth._save_tokens({"access_token": "tok123", "refresh_token": "r"})
    assert fyers_auth.ensure_access_token() == "tok123"


def test_refresh_is_permanently_disabled_no_network(monkeypatch):
    """Fyers disabled programmatic refresh (SEBI, code -16): refresh() must make
    NO network call and always return None."""
    import requests

    def boom(*a, **k):  # any HTTP call here is a bug
        raise AssertionError("refresh() must not hit the network")

    monkeypatch.setattr(requests, "post", boom)
    assert fyers_auth.REFRESH_DISABLED_CODE == -16
    assert "-16" in fyers_auth.REFRESH_DISABLED_MESSAGE
    assert fyers_auth.refresh() is None


def test_stale_token_treated_as_missing_and_alerts(monkeypatch, tmp_path):
    """A token stamped on a previous day is useless (no refresh) — it must be
    treated as MISSING (None) and raise an explicit alert."""
    tokens_file = tmp_path / "tokens.json"
    tokens_file.write_text(
        '{"access_token": "yesterday-tok", "refresh_token": "r", '
        '"saved_at": "2020-01-01T09:00:00"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "FYERS_TOKENS_FILE", tokens_file)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)

    sent: list[str] = []
    monkeypatch.setattr(fyers_auth.alerts, "send", lambda msg: sent.append(msg) or True)

    assert fyers_auth.ensure_access_token() is None
    assert sent and "fyers" in sent[0].lower() and "login" in sent[0].lower()
