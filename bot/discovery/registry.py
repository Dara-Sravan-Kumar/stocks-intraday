"""Registration + retirement of discovered specs.

register_spec is the one door into the fleet: validate -> fleet-cap + duplicate
checks -> backtest_gate -> insert as a DISCOVERED variant. Most proposals are
expected to be REJECTED here — that's the overfitting defense working, not a
bug; every rejection carries a reason and is logged.

DISCOVERED channels get their OWN retire pass (no param-mutation backfill — this
bot has no param-mutation channel): a spec with a net-negative forward-paper
ledger over a real sample is retired.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

import config
from bot import clock, db
from bot.discovery.gate import GateResult, backtest_gate
from bot.discovery.spec import SpecError, StrategySpec, validate_spec

log = logging.getLogger(__name__)


@dataclass
class RegisterResult:
    registered: bool
    reason: str
    name: str = ""
    gate: GateResult | None = None


def _now_iso() -> str:
    return clock.now_ist().isoformat(timespec="seconds")


def register_spec(spec: StrategySpec, *, histories=None,
                  run_gate: bool = True) -> RegisterResult:
    # 1. validate (safety + intraday horizon + channel vocabulary)
    try:
        validate_spec(spec)
    except SpecError as exc:
        return RegisterResult(False, f"invalid: {exc}", spec.name)

    # 2. fleet cap — bound the number of active specs per channel
    if db.count_discovered_specs(spec.channel) >= config.DISCOVERED_FLEET_MAX:
        return RegisterResult(False,
                              f"fleet full ({config.DISCOVERED_FLEET_MAX} active "
                              f"in {spec.channel})", spec.name)

    # 3. duplicate checks — same normalized expr or same name already present
    if spec.canonical_expr() in db.canonical_exprs(spec.channel):
        return RegisterResult(False, "duplicate entry_expr", spec.name)
    if db.discovered_specs(status=None):
        if any(r["name"] == spec.name for r in db.discovered_specs(status=None)):
            return RegisterResult(False, f"name {spec.name!r} already used", spec.name)

    # 4. backtest gate — the overfitting defense (IS/OOS on cached bars)
    gate = GateResult(True, "gate skipped")
    if run_gate:
        gate = backtest_gate(spec, histories=histories)
        if not gate.passed:
            return RegisterResult(False, f"gate: {gate.reason}", spec.name, gate)

    # 5. insert as an ACTIVE variant of its DISCOVERED channel
    db.insert_discovered_spec(
        name=spec.name, channel=spec.channel,
        spec_json=json.dumps(spec.to_dict()),
        entry_expr=spec.entry_expr, canonical_expr=spec.canonical_expr(),
        source=spec.source, gate_json=json.dumps(gate.to_dict()),
        created_at=_now_iso(),
    )
    log.info("registered %s (%s): %s", spec.name, spec.channel, gate.reason)
    return RegisterResult(True, gate.reason, spec.name, gate)


def load_active_specs(channel: str | None = None) -> list[StrategySpec]:
    from bot.discovery.spec import StrategySpec as _S
    out: list[StrategySpec] = []
    for r in db.discovered_specs(channel=channel, status="ACTIVE"):
        try:
            d = json.loads(r["spec_json"])
            out.append(_S(**{k: d[k] for k in d if k in _S.__dataclass_fields__}))
        except Exception as exc:  # noqa: BLE001
            log.warning("bad stored spec %s: %s", r["name"], exc)
    return out


def retire_pass(channel: str, *, min_trades: int | None = None) -> list[str]:
    """Retire ACTIVE specs whose forward-paper ledger is net-negative over a real
    sample. Returns the names retired. No param backfill (discovered specs aren't
    param-mutated — a bad one just leaves the fleet)."""
    min_trades = min_trades or config.DISCOVERED_RETIRE_MIN_TRADES
    retired: list[str] = []
    for r in db.discovered_specs(channel=channel, status="ACTIVE"):
        # pool the ledger across the paper modes this variant could have traded
        agg = {"trades": 0, "net": 0.0}
        for mode in config.DISCOVERED_RETIRE_MODES:
            s = db.variant_stats(r["name"], mode)
            agg["trades"] += s["trades"]
            agg["net"] += s["net"]
        if agg["trades"] >= min_trades and agg["net"] < 0:
            reason = f"net ₹{agg['net']:,.0f} over {agg['trades']} trades"
            db.retire_discovered_spec(r["name"], _now_iso(), reason)
            retired.append(r["name"])
            log.info("retired %s (%s): %s", r["name"], channel, reason)
    return retired
