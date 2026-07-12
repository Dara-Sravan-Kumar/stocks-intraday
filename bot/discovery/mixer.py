"""Phase 4 — the genetic mixer. Breed new specs by AST-clause CROSSOVER of two
parents (mix their AND-conjuncts, dedup, keep 2-4) plus MUTATION (nudge exactly
one numeric literal). The gene pool is the live DISCOVERED specs (fitness-weighted
by their ledger win-rate) plus a SEED_GENES library expressing the bot's proven
intraday channels. Every offspring goes through the SAME backtest gate.

Deterministic under a seeded RNG so breeding is reproducible and testable.
All manipulation is on the AST — literals with digits are never string-mangled.
"""
from __future__ import annotations

import ast
import logging
import random
from dataclasses import dataclass, field

import config
from bot import db
from bot.discovery.registry import RegisterResult, register_spec
from bot.discovery.spec import StrategySpec, missing_fields, validate_spec

log = logging.getLogger(__name__)

# Proven intraday channels expressed as genes: (entry_expr, side, reward_risk).
# EQ genes use the full vocabulary; OPT genes are volume-free (index signals).
SEED_GENES: dict[str, list[tuple[str, str, float]]] = {
    "DISCOVERED_EQ": [
        ("close > or_high and rvol > 1.5", "LONG", 2.0),            # ORB breakout
        ("close > vwap and vwap_dist_pct > 0.1 and rvol > 1.2", "LONG", 2.0),  # VWAP reclaim
        ("rsi2 < 10 and close < vwap", "LONG", 1.5),               # mean-reversion to VWAP
        ("abs(gap_pct) > 0.75 and close > day_open and rvol > 1.5", "LONG", 2.0),  # gap-and-go
        ("day_change_pct > 0.5 and close > ema20 and rvol > 2.0", "LONG", 2.0),   # momentum burst
        ("close > or_high and minutes_since_open < 60", "LONG", 2.0),  # first-hour breakout
        ("close < or_low and rvol > 1.5", "SHORT", 2.0),          # downside ORB
    ],
    "DISCOVERED_OPT": [
        ("close > or_high and or_range_pct > 0.2", "LONG", 2.0),
        ("close < or_low and or_range_pct > 0.2", "SHORT", 2.0),
        ("day_change_pct > 0.4 and close > ema20", "LONG", 2.0),
        ("day_change_pct < -0.4 and close < ema20", "SHORT", 2.0),
        ("close > day_high and minutes_since_open < 75", "LONG", 2.0),
    ],
}

_NUDGE_FACTORS = (0.5, 0.75, 0.9, 1.1, 1.25, 1.5)


@dataclass
class _Gene:
    expr: str
    side: str
    rr: float
    weight: float


@dataclass
class MixReport:
    channel: str
    bred: int = 0
    registered: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.channel}: bred {self.bred}, registered "
                f"{len(self.registered)}, rejected {len(self.rejected)}")


# --- AST operations ---------------------------------------------------------

def and_conjuncts(expr: str) -> list[str]:
    """Split a boolean expr into its top-level AND clauses (normalized)."""
    tree = ast.parse(expr, mode="eval").body
    if isinstance(tree, ast.BoolOp) and isinstance(tree.op, ast.And):
        return [ast.unparse(v) for v in tree.values]
    return [ast.unparse(tree)]


def crossover(expr_a: str, expr_b: str, rng: random.Random,
              min_clauses: int = 2, max_clauses: int = 4) -> str:
    """Mix the AND-conjuncts of two parents, dedup, keep 2-4."""
    seen: dict[str, str] = {}
    for c in and_conjuncts(expr_a) + and_conjuncts(expr_b):
        seen.setdefault(c.replace(" ", ""), c)   # dedup ignoring whitespace
    clauses = list(seen.values())
    rng.shuffle(clauses)
    lo = min(min_clauses, len(clauses))
    hi = min(max_clauses, len(clauses))
    k = rng.randint(lo, hi) if hi >= lo else len(clauses)
    return " and ".join(clauses[:k])


def mutate_one_literal(expr: str, rng: random.Random) -> str:
    """Nudge exactly ONE numeric literal in the expr via the AST."""
    tree = ast.parse(expr, mode="eval")
    consts = [n for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
              and not isinstance(n.value, bool)]
    if not consts:
        return expr
    node = consts[rng.randrange(len(consts))]
    old = float(node.value)
    if old == 0:
        node.value = round(rng.choice((-0.1, 0.1)), 4)
    else:
        node.value = round(old * rng.choice(_NUDGE_FACTORS), 4)
    return ast.unparse(tree)


# --- gene pool + breeding ---------------------------------------------------

def _fitness(channel: str, name: str) -> float:
    """Ledger win-rate as fitness; unproven specs get a small positive floor so
    they can still breed."""
    agg = {"trades": 0, "wins": 0}
    for mode in config.DISCOVERED_RETIRE_MODES:
        s = db.variant_stats(name, mode)
        agg["trades"] += s["trades"]
        agg["wins"] += s["wins"]
    if agg["trades"] < 5:
        return 0.5
    return max(0.1, agg["wins"] / agg["trades"])


def build_gene_pool(channel: str) -> list[_Gene]:
    pool = [_Gene(expr, side, rr, weight=1.0)
            for expr, side, rr in SEED_GENES.get(channel, [])]
    for r in db.discovered_specs(channel=channel, status="ACTIVE"):
        import json
        try:
            d = json.loads(r["spec_json"])
        except Exception:  # noqa: BLE001
            continue
        pool.append(_Gene(d["entry_expr"], d.get("side", "LONG"),
                          float(d.get("min_reward_risk", 2.0)),
                          weight=1.0 + _fitness(channel, r["name"])))
    return pool


def _pick_two(pool: list[_Gene], rng: random.Random) -> tuple[_Gene, _Gene]:
    a = rng.choices(pool, weights=[g.weight for g in pool], k=1)[0]
    rest = [g for g in pool if g is not a] or pool
    b = rng.choices(rest, weights=[g.weight for g in rest], k=1)[0]
    return a, b


def breed(channel: str = "DISCOVERED_EQ", *, n: int | None = None,
          seed: int | None = None, run_gate: bool = True,
          histories=None) -> MixReport:
    """Breed and register up to n offspring for `channel`. Deterministic given
    `seed`. Every offspring passes through register_spec (validate + gate)."""
    n = n or config.MIXER_OFFSPRING_PER_RUN
    rng = random.Random(config.MIXER_RNG_SEED if seed is None else seed)
    report = MixReport(channel=channel)
    pool = build_gene_pool(channel)
    if len(pool) < 2:
        return report

    for i in range(n):
        a, b = _pick_two(pool, rng)
        try:
            child_expr = mutate_one_literal(crossover(a.expr, b.expr, rng), rng)
        except Exception as exc:  # noqa: BLE001
            report.rejected.append((f"mix_{i}", f"bad crossover: {exc}"))
            continue
        name = f"mix_{channel.split('_')[-1].lower()}_{i}_{rng.randrange(1000, 9999)}"
        spec = StrategySpec(
            name=name, entry_expr=child_expr, channel=channel,
            side=a.side, min_reward_risk=round((a.rr + b.rr) / 2.0, 2),
            source="mixer", rationale=f"crossover of proven clauses (gen seed)",
            parents=[a.expr, b.expr],
        )
        report.bred += 1
        if missing_fields(spec):
            report.rejected.append((name, "off-vocabulary after crossover"))
            continue
        try:
            validate_spec(spec)
        except Exception as exc:  # noqa: BLE001
            report.rejected.append((name, f"invalid: {exc}"))
            continue
        res: RegisterResult = register_spec(spec, run_gate=run_gate, histories=histories)
        if res.registered:
            report.registered.append(name)
        else:
            report.rejected.append((name, res.reason))
    log.info("mixer %s", report.summary())
    return report
