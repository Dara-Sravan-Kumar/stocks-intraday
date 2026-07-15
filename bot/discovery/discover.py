"""Phase 3 — the discoverer. Ask Claude (via the subscription CLI `claude -p`,
NOT the paid API) for published INTRADAY strategies, translate each to a spec,
and push it through register_spec (validate + gate).

Two outcomes per proposal: registered, or rejected with a reason. A proposal
that needs an indicator we don't compute is rejected naming the missing field —
a signal to add that indicator later.

The LLM writes DATA (a boolean entry_expr), never code: it is parsed, validated
by the whitelist-only interpreter, and gated before it can ever fire.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field

import config
from bot import db
from bot.discovery.registry import RegisterResult, register_spec
from bot.discovery.spec import missing_fields, spec_from_dict
from bot.discovery.vocab import channel_vocab

log = logging.getLogger(__name__)

# Human glossary for the fields the model is most likely to reach for. Fields not
# listed are still offered (bare) so the vocabulary stays auto-derived.
_GLOSSARY = {
    "close": "latest 5m close",
    "day_open": "session open price",
    "or_high": "opening-range high (first 15 min)",
    "or_low": "opening-range low",
    "or_range_pct": "opening-range width as % of price",
    "gap_pct": "open vs prior close, %",
    "day_change_pct": "close vs day open, %",
    "day_range_pct": "high-low as % of price",
    "vwap": "session VWAP (equity only; None on indices)",
    "vwap_dist_pct": "distance of close from VWAP, % (equity only)",
    "rvol": "cumulative volume vs its historical profile (equity only)",
    "rsi14": "Wilder RSI(14) on 5m",
    "rsi2": "RSI(2) — fast mean-reversion trigger",
    "ema20": "20-period EMA on 5m",
    "atr_pct": "ATR(14) as % of price",
    "minutes_since_open": "minutes elapsed in the session",
}

_INTRADAY_RULES = """HARD RULES (a proposal breaking any of these is useless):
- INTRADAY ONLY. The position opens and CLOSES within the same session; it is
  squared off before market close. NO overnight or multi-day holds. NO
  swing/positional/fundamental ideas.
- The entry_expr is a BOOLEAN expression over ONLY the allowed field names
  below, numeric literals, the operators < <= > >= == != and + - * / %, the
  words and/or/not, and the calls min()/max()/abs(). Nothing else — no function
  names, no attributes, no indexing.
- Prefer expressions of 2-4 clauses joined by 'and'. Keep thresholds realistic
  for Indian large-cap intraday on 5-minute bars."""

_EXAMPLES = ("opening-range breakout, VWAP reclaim/bounce, gap-and-go, momentum "
             "burst, first-hour breakout, volume-spike breakout, "
             "mean-reversion to VWAP")


@dataclass
class DiscoverReport:
    channel: str
    proposed: int = 0
    registered: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)   # (name, reason)
    raw_ok: bool = True

    def summary(self) -> str:
        return (f"{self.channel}: proposed {self.proposed}, "
                f"registered {len(self.registered)}, rejected {len(self.rejected)}")


def _performance_digest(mode: str, *, top: int = 3) -> str:
    """Compact read of how the live fleet is ACTUALLY doing on `mode`: overall
    win rate plus the best/worst variants by profit factor. Fed into the
    discovery prompt so proposals answer what's really failing on this book.
    Returns '' when there's no closed-trade history yet."""
    rows = [r for r in db.variant_ledger_stats(mode) if r["trades"]]
    if not rows:
        return ""

    def pf(r) -> float:
        gl = r["gross_loss"]
        return float("inf") if gl <= 0 else r["gross_win"] / gl

    def line(r) -> str:
        gl = r["gross_loss"]
        pf_s = "inf" if gl <= 0 else f"{r['gross_win'] / gl:.2f}"
        wr = r["wins"] / r["trades"] * 100.0 if r["trades"] else 0.0
        return (f"  - {r['variant_key']}: {r['trades']} trades, {wr:.0f}% win, "
                f"PF {pf_s}, net Rs{r['net']:,.0f}")

    ranked = sorted(rows, key=pf, reverse=True)
    total = sum(r["trades"] for r in rows)
    wins = sum(r["wins"] for r in rows)
    overall_wr = wins / total * 100.0 if total else 0.0
    parts = [f"  Overall: {total} closed trades, {overall_wr:.0f}% win rate.",
             "  Best variants (by profit factor):"]
    parts += [line(r) for r in ranked[:top]]
    parts.append("  Worst variants:")
    parts += [line(r) for r in ranked[-top:]]
    return "\n".join(parts)


def build_prompt(channel: str, existing_exprs: list[str], n: int, *,
                 performance: str | None = None,
                 lessons: list[str] | None = None,
                 web: bool = False) -> str:
    vocab = sorted(channel_vocab(channel))
    lines = [f"- {f}: {_GLOSSARY[f]}" if f in _GLOSSARY else f"- {f}" for f in vocab]
    where = ("on NIFTY/BANKNIFTY INDEX structure — you buy the ATM option, so "
             "use price-structure fields only (no volume: vwap/rvol are unavailable)"
             if channel == "DISCOVERED_OPT"
             else "on individual NSE large-cap STOCKS (full vocabulary available)")
    existing = "\n".join(f"- {e}" for e in existing_exprs[:40]) or "(none yet)"
    source = (
        "SEARCH THE WEB for INTRADAY trading strategies traders/quants are "
        "currently publishing for NSE India / global equities (recent articles, "
        "blogs, papers) — then translate the most promising same-day ones into "
        "specs."
        if web else
        f"Draw on published intraday playbooks such as: {_EXAMPLES}."
    )
    context = ""
    if performance:
        context += ("\nHow this bot's live fleet is ACTUALLY performing "
                    "(bias proposals toward fixing the worst):\n" + performance + "\n")
    if lessons:
        context += ("\nLessons from a post-mortem of this book's own recent closed "
                    "trades — bias your proposals to ADDRESS these:\n"
                    + "\n".join(f"  - {x}" for x in lessons) + "\n")
    return f"""You are proposing published, well-known INTRADAY trading strategies for an
automated Indian-market paper-trading bot. Signals are evaluated {where}.

{_INTRADAY_RULES}

ALLOWED FIELD NAMES (the ONLY names you may use):
{chr(10).join(lines)}

{source}
{context}
Already-registered entry_exprs (propose DISTINCT ideas, not variants of these):
{existing}

Return STRICT JSON, no prose, no code fence:
{{"strategies": [
  {{"name": "snake_case_name", "entry_expr": "<boolean expr>",
    "side": "LONG" or "SHORT", "min_reward_risk": <number 1.0-3.0>,
    "rationale": "<one sentence: the published edge>"}}
]}}
Propose {n} strategies."""


def _claude_cli(prompt: str, *, allowed_tools: list[str] | None = None,
                timeout: int | None = None) -> str:
    """Call the Claude CLI in print mode. Subscription-billed, NOT the paid API.

    `allowed_tools` whitelists Claude Code tools for this call (e.g. ["WebSearch"]);
    in headless `-p` mode a tool not on the list is auto-denied, never prompted.
    `timeout` overrides the default for slower tool-using (web) calls."""
    cli = shutil.which(config.CLAUDE_CLI) or config.CLAUDE_CLI
    argv = [cli, "-p", "--model", config.DISCOVERY_LLM_MODEL]
    if allowed_tools:
        # value is comma-joined; the prompt goes on STDIN (not a positional) so
        # a multi-value tools flag can't swallow it.
        argv += ["--allowedTools", ",".join(allowed_tools)]
    proc = subprocess.run(
        argv, input=prompt, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=timeout or config.DISCOVERY_TIMEOUT_SEC,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI exit {proc.returncode}: {(proc.stderr or '')[:400]}")
    return proc.stdout


def _extract_json(text: str, *, require_key: str | None = "strategies") -> dict:
    """Pull the first {...} object out of a CLI response, tolerating code
    fences/prose. When `require_key` is set, only an object containing that key
    is accepted (so a stray JSON snippet in prose is skipped)."""
    if not text:
        raise ValueError("empty response")
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict) and (require_key is None
                                                      or require_key in obj):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise ValueError(f"no JSON object with '{require_key}' found in response")


def discover_and_register(channel: str = "DISCOVERED_EQ", *, n: int | None = None,
                          caller=None, run_gate: bool = True,
                          histories=None, lessons: list[str] | None = None,
                          performance: str | None = None,
                          web: bool | None = None) -> DiscoverReport:
    """Propose N strategies for `channel` and register those that pass the gate.

    `caller(prompt)->str` is injectable so tests never spawn the CLI. `lessons`
    (from the daily post-mortem) and `performance` (the live-fleet digest) are
    woven into the prompt so proposals target what's actually failing. `web`
    enables WebSearch (defaults to config.DISCOVERY_WEB_ENABLED)."""
    n = n or config.DISCOVERY_N_PER_RUN
    caller = caller or _claude_cli
    if web is None:
        web = getattr(config, "DISCOVERY_WEB_ENABLED", False)
    report = DiscoverReport(channel=channel)

    existing = [r["entry_expr"] for r in db.discovered_specs(channel=channel, status=None)]
    prompt = build_prompt(channel, existing, n, performance=performance,
                          lessons=lessons, web=web)
    try:
        # Only pass tool/timeout kwargs to the real CLI caller; injected test
        # callers take just the prompt.
        if caller is _claude_cli and web:
            raw = caller(prompt, allowed_tools=["WebSearch"],
                         timeout=config.DISCOVERY_WEB_TIMEOUT_SEC)
        else:
            raw = caller(prompt)
        payload = _extract_json(raw)
    except Exception as exc:  # noqa: BLE001 — a discovery failure must never crash the run
        log.warning("discovery(%s) failed: %s", channel, exc)
        report.raw_ok = False
        return report

    strategies = payload.get("strategies") or []
    report.proposed = len(strategies)
    for raw_spec in strategies:
        try:
            spec = spec_from_dict(raw_spec, channel=channel, source="discovered")
        except Exception as exc:  # noqa: BLE001
            report.rejected.append(("?", f"unparseable: {exc}"))
            continue
        miss = missing_fields(spec)
        if miss:
            report.rejected.append((spec.name, f"needs indicator(s): {', '.join(sorted(miss))}"))
            continue
        res: RegisterResult = register_spec(spec, run_gate=run_gate, histories=histories)
        if res.registered:
            report.registered.append(spec.name)
        else:
            report.rejected.append((spec.name, res.reason))
    log.info("discovery %s", report.summary())
    return report
