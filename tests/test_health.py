"""Task 2 — run-health failure classification (string/flag matching)."""
from __future__ import annotations

from bot import health


def test_no_feed_failure_when_fyers_not_required():
    # dev runs (--feed yf / dhan / replay) intentionally aren't on Fyers.
    assert health.feed_failures("yfinance", require_fyers=False) == []


def test_healthy_fyers_feed_is_no_failure():
    assert health.feed_failures("fyers-ws", require_fyers=True) == []


def test_degraded_feed_flagged_with_login_remedy():
    out = health.feed_failures("yfinance (degraded from fyers)", require_fyers=True)
    assert len(out) == 1
    assert "LIVE DATA FEED" in out[0]["kind"]
    assert "login" in out[0]["detail"].lower()


def test_aborted_feed_flagged():
    out = health.feed_failures("fyers-ws (aborted: feed failure)", require_fyers=True)
    assert len(out) == 1 and "ABORTED" in out[0]["detail"]


def test_discovery_failures_pass_through():
    rep = {"failures": [{"kind": "LLM / DISCOVERY", "detail": "no proposals"}]}
    assert health.discovery_failures(rep) == rep["failures"]
    assert health.discovery_failures(None) == []
    assert health.discovery_failures({"skipped": "x"}) == []


def test_collect_composes_feed_and_discovery():
    out = health.collect_failures(
        feed_source="yfinance (degraded from fyers)", require_fyers=True,
        discovery_report={"failures": [{"kind": "LLM / DISCOVERY", "detail": "x"}]},
    )
    kinds = {f["kind"] for f in out}
    assert any("LIVE DATA FEED" in k for k in kinds)
    assert "LLM / DISCOVERY" in kinds
