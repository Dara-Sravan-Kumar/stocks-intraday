"""Every live-trading gate must independently block real orders."""
from __future__ import annotations

from datetime import datetime

import pytest

import config
from bot.clock import IST
from bot.execution import LONG
from bot.execution.dhan_broker import (
    HybridBroker, LiveTradingBlocked, check_live_gates,
)
from bot.execution.paper_broker import PaperBroker


class FakeClient:
    NSE = "NSE_EQ"
    BUY = "BUY"
    SELL = "SELL"
    MARKET = "MARKET"
    INTRA = "INTRADAY"

    def __init__(self):
        self.orders = []

    def place_order(self, **kw):
        self.orders.append(kw)
        return {"status": "success", "data": {"orderId": f"o{len(self.orders)}"}}


def all_gates_open(monkeypatch):
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(config, "LIVE_STRATEGY_ALLOWLIST", {"orb"})
    monkeypatch.setenv("DHAN_LIVE_CONFIRM", config.LIVE_CONFIRM_STRING)
    monkeypatch.setenv("DHAN_CLIENT_ID", "test-client")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test-token")


def ts():
    return datetime(2026, 7, 6, 10, 0, tzinfo=IST)


def test_gate1_disabled_blocks(monkeypatch):
    all_gates_open(monkeypatch)
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", False)
    with pytest.raises(LiveTradingBlocked, match="gate 1"):
        check_live_gates()


def test_gate2_confirm_string_blocks(monkeypatch):
    all_gates_open(monkeypatch)
    monkeypatch.setenv("DHAN_LIVE_CONFIRM", "yes")   # wrong string
    with pytest.raises(LiveTradingBlocked, match="gate 2"):
        check_live_gates()


def test_gate3_missing_credentials_blocks(monkeypatch):
    all_gates_open(monkeypatch)
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "")
    with pytest.raises(LiveTradingBlocked, match="gate 3"):
        check_live_gates()


def test_gate5_empty_allowlist_blocks(monkeypatch):
    all_gates_open(monkeypatch)
    monkeypatch.setattr(config, "LIVE_STRATEGY_ALLOWLIST", set())
    with pytest.raises(LiveTradingBlocked, match="gate 5"):
        check_live_gates()


def test_default_config_is_fully_locked():
    """The shipped defaults must block live trading outright."""
    assert config.LIVE_TRADING_ENABLED is False
    assert config.LIVE_STRATEGY_ALLOWLIST == set()


def test_allowlisted_strategy_mirrors_order(monkeypatch, mem_db):
    all_gates_open(monkeypatch)
    client = FakeClient()
    broker = HybridBroker(PaperBroker(100_000), client=client,
                          security_ids={"RELIANCE": "2885"})
    pos = broker.open_position("orb", "RELIANCE", LONG, 100, 500.0, ts(),
                               stop=495.0, target=510.0)
    assert pos is not None
    assert len(client.orders) == 1                      # live mirror fired
    assert client.orders[0]["transaction_type"] == "BUY"
    # scaled: 100 * (25k/~100k) * (0.25/0.5) = ~12
    assert pos.scratch["live_qty"] == client.orders[0]["quantity"] > 0

    broker.close_position(pos, 510.0, ts(), "TARGET")
    assert len(client.orders) == 2
    assert client.orders[1]["transaction_type"] == "SELL"
    assert client.orders[1]["quantity"] == client.orders[0]["quantity"]


def test_non_allowlisted_strategy_stays_paper(monkeypatch, mem_db):
    all_gates_open(monkeypatch)
    client = FakeClient()
    broker = HybridBroker(PaperBroker(100_000), client=client,
                          security_ids={"RELIANCE": "2885"})
    pos = broker.open_position("gap", "RELIANCE", LONG, 100, 500.0, ts(),
                               stop=495.0, target=510.0)
    assert pos is not None
    assert client.orders == []                          # no live order
    assert "live_qty" not in pos.scratch


def test_live_concurrent_cap(monkeypatch, mem_db):
    all_gates_open(monkeypatch)
    monkeypatch.setattr(config, "LIVE_MAX_CONCURRENT_POSITIONS", 1)
    client = FakeClient()
    broker = HybridBroker(PaperBroker(100_000), client=client,
                          security_ids={"A": "1", "B": "2"})
    broker.open_position("orb", "A", LONG, 100, 500.0, ts(), 495.0, 510.0)
    pos2 = broker.open_position("orb", "B", LONG, 100, 500.0, ts(), 495.0, 510.0)
    assert len(client.orders) == 1                      # second entry paper-only
    assert "live_qty" not in pos2.scratch
