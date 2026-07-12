"""A strategy as DATA. A StrategySpec is a JSON-serializable record whose
entry_expr is a boolean expression over the intraday snapshot vocabulary.

Registration is gated on horizon == "INTRADAY": this bot squares off every
session, so swing / positional / multi-day / fundamental strategies are refused
outright — they can never be represented here.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from bot.discovery.expr import CompiledExpr, ExprError, validate_expr
from bot.discovery.vocab import channel_vocab

HORIZON = "INTRADAY"
CHANNELS = ("DISCOVERED_EQ", "DISCOVERED_OPT")
SIDES = ("LONG", "SHORT")
_NAME_RE = re.compile(r"^[a-z0-9_]{3,48}$")


class SpecError(ValueError):
    """A spec is malformed or violates the intraday/vocabulary rules."""


@dataclass
class StrategySpec:
    name: str
    entry_expr: str
    horizon: str = HORIZON
    side: str = "LONG"                    # LONG buys stock / CE; SHORT -> PE
    min_reward_risk: float = 1.5
    source: str = "manual"               # manual | discovered | mixer
    rationale: str = ""
    channel: str = "DISCOVERED_EQ"       # DISCOVERED_EQ | DISCOVERED_OPT
    underlying: str | None = None        # NIFTY | BANKNIFTY for OPT specs
    parents: list[str] = field(default_factory=list)   # mixer lineage

    def to_dict(self) -> dict:
        return asdict(self)

    def canonical_expr(self) -> str:
        """Whitespace-normalized entry_expr for duplicate detection."""
        return re.sub(r"\s+", " ", self.entry_expr).strip()


def spec_from_dict(d: dict, *, channel: str = "DISCOVERED_EQ",
                   source: str = "discovered") -> StrategySpec:
    """Translate a raw {name, entry_expr, min_reward_risk, rationale, ...} dict
    (e.g. one item from the LLM's JSON) into a StrategySpec. Does not validate —
    call validate_spec / register_spec next."""
    underlying = d.get("underlying")
    if channel == "DISCOVERED_OPT" and not underlying:
        underlying = "NIFTY"
    return StrategySpec(
        name=str(d.get("name", "")).strip().lower().replace(" ", "_")[:48],
        entry_expr=str(d.get("entry_expr", "")).strip(),
        horizon=str(d.get("horizon", HORIZON)).strip().upper() or HORIZON,
        side=str(d.get("side", "LONG")).strip().upper() or "LONG",
        min_reward_risk=float(d.get("min_reward_risk", 1.5) or 1.5),
        source=source,
        rationale=str(d.get("rationale", "")).strip()[:500],
        channel=channel,
        underlying=underlying,
        parents=list(d.get("parents", []) or []),
    )


def validate_spec(spec: StrategySpec) -> CompiledExpr:
    """Full structural + safety validation. Returns the compiled entry_expr so
    callers don't re-parse. Raises SpecError on any violation."""
    if not _NAME_RE.match(spec.name or ""):
        raise SpecError(f"bad name {spec.name!r} (need [a-z0-9_], 3-48 chars)")
    if spec.horizon != HORIZON:
        raise SpecError(f"horizon must be {HORIZON!r}, got {spec.horizon!r} "
                        "(this bot cannot hold overnight)")
    if spec.channel not in CHANNELS:
        raise SpecError(f"unknown channel {spec.channel!r}")
    if spec.side not in SIDES:
        raise SpecError(f"side must be one of {SIDES}, got {spec.side!r}")
    if not (0.1 <= spec.min_reward_risk <= 20.0):
        raise SpecError(f"min_reward_risk {spec.min_reward_risk} out of range")
    if spec.channel == "DISCOVERED_OPT" and spec.underlying not in ("NIFTY", "BANKNIFTY"):
        raise SpecError(f"OPT spec needs underlying NIFTY|BANKNIFTY, got {spec.underlying!r}")
    try:
        return validate_expr(spec.entry_expr, channel_vocab(spec.channel))
    except ExprError as exc:
        raise SpecError(str(exc)) from exc


def missing_fields(spec: StrategySpec) -> set[str]:
    """Names the expr references that aren't in this channel's vocabulary — a
    signal to add that indicator later. Empty for a valid spec."""
    import ast
    try:
        tree = ast.parse(spec.entry_expr, mode="eval")
    except SyntaxError:
        return set()
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    return used - set(channel_vocab(spec.channel)) - {"min", "max", "abs"}
