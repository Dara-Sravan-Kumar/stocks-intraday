"""Phase 1 — strategy-as-data + the whitelist-only interpreter.

The SAFETY block is the reason this system is allowed to run LLM/bred text:
malicious or unknown expressions are refused at validate() time and therefore
never reach the evaluator.
"""
from __future__ import annotations

import pytest

from bot.discovery.expr import (
    CompiledExpr,
    ExprError,
    eval_expr,
    validate_expr,
)
from bot.discovery.spec import SpecError, StrategySpec, missing_fields, validate_spec
from bot.discovery.vocab import EQUITY_VOCAB, INDEX_VOCAB, Snapshot

VOCAB = EQUITY_VOCAB


def ev(expr: str, **env) -> bool:
    compiled = validate_expr(expr, VOCAB)
    full = {k: None for k in VOCAB}
    full.update(env)
    return eval_expr(compiled, full)


# --- SAFETY: refused AND never executed -------------------------------------

MALICIOUS = [
    "__import__('os').system('echo hi')",
    "open('/etc/passwd').read()",
    "close.__class__.__mro__",
    "close.__globals__",
    "().__class__.__bases__",
    "close[0]",
    "(lambda: 1)()",
    "[x for x in range(3)]",
    "{x for x in range(3)}",
    "{'a': 1}",
    "close if close else 0",          # IfExp not allowed
    "exec('x=1')",
    "eval('1')",
    "len(close)",                      # non-whitelisted call
    "print(close)",
    "close and unknown_field",         # unknown name
    "getattr(close, 'x')",
    "close := 5",                      # walrus
    "f'{close}'",                      # f-string
    "1 ; 2",                           # multiple statements -> SyntaxError in eval mode
]


@pytest.mark.parametrize("expr", MALICIOUS)
def test_malicious_expressions_are_refused(expr):
    with pytest.raises(ExprError):
        validate_expr(expr, VOCAB)


def test_refused_expression_is_never_evaluated(monkeypatch):
    """Even a validated-looking payload can't slip into _ev: validate raises
    first, so eval_expr is never called on unsafe input."""
    import bot.discovery.expr as exprmod
    called = {"n": 0}
    real_ev = exprmod._ev

    def counting_ev(node, env):
        called["n"] += 1
        return real_ev(node, env)

    monkeypatch.setattr(exprmod, "_ev", counting_ev)
    with pytest.raises(ExprError):
        validate_expr("__import__('os').system('x')", VOCAB)
    assert called["n"] == 0     # never entered the evaluator


# --- correct evaluation ------------------------------------------------------

def test_digit_bearing_field_names_preserved():
    compiled = validate_expr("rsi14 < 30 and rsi2 < 10", VOCAB)
    assert compiled.names == {"rsi14", "rsi2"}
    assert ev("rsi14 < 30 and rsi2 < 10", rsi14=25.0, rsi2=5.0) is True
    assert ev("rsi14 < 30 and rsi2 < 10", rsi14=40.0, rsi2=5.0) is False


def test_orb_breakout_expr():
    assert ev("close > or_high and day_change_pct > 0.3",
              close=101.0, or_high=100.0, day_change_pct=0.5) is True
    assert ev("close > or_high and day_change_pct > 0.3",
              close=99.0, or_high=100.0, day_change_pct=0.5) is False


def test_min_max_abs_calls():
    assert ev("abs(day_change_pct) > 1.0", day_change_pct=-1.5) is True
    assert ev("max(rsi7, rsi14) > 70", rsi7=60.0, rsi14=75.0) is True
    assert ev("min(rsi7, rsi14) > 70", rsi7=60.0, rsi14=75.0) is False


def test_or_and_not():
    assert ev("close > vwap or rsi2 < 5", close=90.0, vwap=100.0, rsi2=3.0) is True
    assert ev("not (close > vwap)", close=90.0, vwap=100.0) is True


def test_none_operand_yields_no_signal_not_crash():
    # rvol not computed yet (None) -> comparison False, whole AND False, no raise
    assert ev("close > or_high and rvol > 2.0", close=101.0, or_high=100.0) is False
    # None in arithmetic poisons to None -> compare False
    assert ev("close - vwap > 0", close=101.0) is False


def test_division_by_zero_is_none_safe():
    assert ev("close / 0 > 1", close=100.0) is False


# --- spec validation ---------------------------------------------------------

def test_horizon_must_be_intraday():
    spec = StrategySpec(name="swinger", entry_expr="close > or_high",
                        horizon="SWING")
    with pytest.raises(SpecError, match="INTRADAY"):
        validate_spec(spec)


def test_valid_equity_spec():
    spec = StrategySpec(name="orb_break", entry_expr="close > or_high and rvol > 1.5",
                        channel="DISCOVERED_EQ")
    compiled = validate_spec(spec)
    assert isinstance(compiled, CompiledExpr)


def test_opt_spec_cannot_use_volume_fields():
    """DISCOVERED_OPT fires on the index, which has no volume — vwap/rvol are
    off-vocabulary and rejected."""
    spec = StrategySpec(name="opt_vwap", entry_expr="close > vwap",
                        channel="DISCOVERED_OPT", underlying="NIFTY")
    with pytest.raises(SpecError):
        validate_spec(spec)
    assert "vwap" not in INDEX_VOCAB


def test_opt_spec_needs_underlying():
    spec = StrategySpec(name="opt_orb2", entry_expr="close > or_high",
                        channel="DISCOVERED_OPT", underlying=None)
    with pytest.raises(SpecError, match="underlying"):
        validate_spec(spec)


def test_missing_fields_reports_unknown_indicator():
    spec = StrategySpec(name="needs_supertrend",
                        entry_expr="close > supertrend and rvol > 2")
    assert missing_fields(spec) == {"supertrend"}


def test_vocab_autoderives_from_snapshot():
    assert EQUITY_VOCAB == frozenset(f for f in Snapshot().as_env())
    assert "rsi14" in EQUITY_VOCAB and "vwap" in EQUITY_VOCAB
