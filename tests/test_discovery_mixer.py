"""Phase 4 — genetic mixer: AST crossover + single-literal mutation, seeded."""
from __future__ import annotations

import ast
import random

from bot.discovery.mixer import (
    SEED_GENES,
    and_conjuncts,
    breed,
    build_gene_pool,
    crossover,
    mutate_one_literal,
)
from bot.discovery.registry import load_active_specs
from bot.discovery.spec import validate_spec
from bot.discovery.vocab import EQUITY_VOCAB


def _literals(expr: str) -> list[float]:
    tree = ast.parse(expr, mode="eval")
    return [float(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
            and not isinstance(n.value, bool)]


def _names(expr: str) -> set[str]:
    tree = ast.parse(expr, mode="eval")
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - {"min", "max", "abs"}


# --- AST operations ---------------------------------------------------------

def test_and_conjuncts_splits_top_level_ands():
    assert and_conjuncts("close > or_high and rvol > 1.5 and rsi14 > 50") == [
        "close > or_high", "rvol > 1.5", "rsi14 > 50",
    ]
    # a non-AND expr is a single clause
    assert and_conjuncts("close > vwap or rsi2 < 5") == ["close > vwap or rsi2 < 5"]


def test_crossover_mixes_and_dedups_clauses():
    rng = random.Random(1)
    child = crossover("close > or_high and rvol > 1.5",
                      "close > or_high and rsi14 > 50", rng)
    clauses = and_conjuncts(child)
    assert 2 <= len(clauses) <= 4
    # 'close > or_high' appears in both parents but only once in the child
    assert sum(c == "close > or_high" for c in clauses) <= 1
    # every child clause came from a parent
    assert set(clauses) <= {"close > or_high", "rvol > 1.5", "rsi14 > 50"}


def test_crossover_is_deterministic_under_seed():
    a, b = "close > or_high and rvol > 1.5", "rsi2 < 10 and close < vwap"
    assert crossover(a, b, random.Random(42)) == crossover(a, b, random.Random(42))


def test_mutation_changes_exactly_one_literal_preserving_names():
    expr = "rsi14 < 30 and vwap_dist_pct > 0.1 and rvol > 1.5"
    mutated = mutate_one_literal(expr, random.Random(3))
    assert _names(mutated) == _names(expr)          # rsi14/vwap_dist_pct intact
    before, after = _literals(expr), _literals(mutated)
    assert len(before) == len(after)
    differ = sum(1 for x, y in zip(sorted(before), sorted(after)) if x != y)
    assert differ == 1                               # exactly one literal nudged


def test_mutation_noop_when_no_literals():
    assert mutate_one_literal("close > vwap and close > ema20", random.Random(1)) \
        == "close > vwap and close > ema20"


# --- gene pool + breeding ---------------------------------------------------

def test_seed_genes_are_valid_specs():
    from bot.discovery.spec import StrategySpec
    for channel, genes in SEED_GENES.items():
        for expr, side, rr in genes:
            spec = StrategySpec(name="seed_check", entry_expr=expr, channel=channel,
                                side=side, min_reward_risk=rr,
                                underlying="NIFTY" if channel == "DISCOVERED_OPT" else None)
            validate_spec(spec)   # must not raise


def test_build_gene_pool_includes_seeds(mem_db):
    pool = build_gene_pool("DISCOVERED_EQ")
    assert len(pool) == len(SEED_GENES["DISCOVERED_EQ"])


def test_breed_registers_valid_offspring(mem_db):
    report = breed("DISCOVERED_EQ", n=5, seed=7, run_gate=False)
    assert report.bred == 5
    assert len(report.registered) >= 1
    active = load_active_specs("DISCOVERED_EQ")
    assert {s.name for s in active} == set(report.registered)
    for s in active:
        assert s.source == "mixer" and s.parents
        # every bred expr stays inside the vocabulary
        assert _names(s.entry_expr) <= set(EQUITY_VOCAB)


def test_breed_is_deterministic(mem_db):
    r1 = breed("DISCOVERED_EQ", n=4, seed=99, run_gate=False)
    from bot import db
    db.set_db_path(":memory:"); db.connect()   # fresh DB, same seed
    r2 = breed("DISCOVERED_EQ", n=4, seed=99, run_gate=False)
    assert r1.registered == r2.registered
