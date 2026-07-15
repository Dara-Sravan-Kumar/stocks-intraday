"""Task 1b — discovery is seeded with post-mortem lessons + a live-performance
digest, and can enable WebSearch."""
from __future__ import annotations

from bot import db
from bot.discovery.discover import _performance_digest, build_prompt


def test_prompt_injects_performance_and_lessons_and_web():
    p = build_prompt("DISCOVERED_EQ", [], 5,
                     performance="  Overall: 10 closed trades, 30% win rate.",
                     lessons=["stops too tight", "avoid opening whipsaw"],
                     web=True)
    assert "SEARCH THE WEB" in p
    assert "ACTUALLY performing" in p and "30% win rate" in p
    assert "ADDRESS these" in p
    assert "stops too tight" in p and "avoid opening whipsaw" in p


def test_prompt_offline_when_web_false():
    p = build_prompt("DISCOVERED_EQ", [], 5, web=False)
    assert "SEARCH THE WEB" not in p
    assert "published intraday playbooks" in p


def _trade(variant: str, net: float):
    db.record_trade(
        run_id=None, mode="PAPER", strategy="s", variant_key=variant,
        symbol="X", side="LONG", qty=1, entry_ts="2026-07-06T10:00:00+05:30",
        entry_price=100.0, exit_ts="2026-07-06T10:30:00+05:30", exit_price=101.0,
        gross_pnl=net, costs=0.0, net_pnl=net, r_multiple=1.0,
        planned_stop=99.0, planned_target=102.0, exit_reason="TARGET",
        feed_source="fyers-ws",
    )


def test_performance_digest_ranks_by_profit_factor(mem_db):
    for _ in range(4):
        _trade("winner", 300.0)
    for _ in range(4):
        _trade("loser", -200.0)
    digest = _performance_digest("PAPER")
    assert "Overall: 8 closed trades" in digest
    assert "Best variants" in digest and "Worst variants" in digest
    assert "winner" in digest and "loser" in digest


def test_performance_digest_empty_without_trades(mem_db):
    assert _performance_digest("PAPER") == ""
