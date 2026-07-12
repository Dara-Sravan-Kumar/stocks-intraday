"""Self-improving strategy fleet: strategies represented as DATA (a JSON spec
with a boolean entry_expr), evaluated by a whitelist-only AST interpreter — no
eval/exec, ever. Discovered/bred specs run as extra variants in two DISCOVERED
channels, each gated by an in-sample/out-of-sample backtest on cached bars.

INTRADAY ONLY: every spec must declare horizon == "INTRADAY"; positions open
and square off within the session. Swing / positional / fundamental strategies
are rejected at registration.
"""
from __future__ import annotations

from bot.discovery.expr import ExprError, compile_expr, eval_expr, validate_expr
from bot.discovery.spec import (
    SpecError,
    StrategySpec,
    spec_from_dict,
    validate_spec,
)
from bot.discovery.vocab import (
    EQUITY_VOCAB,
    INDEX_VOCAB,
    Snapshot,
    build_snapshot,
    channel_vocab,
)

__all__ = [
    "ExprError", "compile_expr", "eval_expr", "validate_expr",
    "SpecError", "StrategySpec", "spec_from_dict", "validate_spec",
    "EQUITY_VOCAB", "INDEX_VOCAB", "Snapshot", "build_snapshot", "channel_vocab",
]
