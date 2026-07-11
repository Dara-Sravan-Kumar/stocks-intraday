"""End-of-day report and promotion-readiness table."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from rich.console import Console
from rich.table import Table

import config
from bot import db


def _trade_stats(trades: list) -> dict:
    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "gross": sum(t["gross_pnl"] for t in trades),
        "costs": sum(t["costs"] for t in trades),
        "net": sum(t["net_pnl"] for t in trades),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "expectancy": sum(t["net_pnl"] for t in trades) / len(trades) if trades else 0.0,
        "avg_r": sum(rs) / len(rs) if rs else None,
    }


def eod_report(mode: str, session_date: str, console: Console | None = None) -> str:
    """Render + persist today's per-strategy stats. Returns a plain-text summary."""
    console = console or Console()
    trades = [t for t in db.trades_for(mode, since_date=session_date)
              if t["exit_ts"][:10] == session_date]
    by_strategy: dict[str, list] = defaultdict(list)
    for t in trades:
        by_strategy[t["strategy"]].append(t)

    table = Table(title=f"EOD {mode} — {session_date}")
    for col in ("Strategy", "Trades", "Win%", "Gross", "Costs", "Net", "Avg R", "PF"):
        table.add_column(col, justify="right")
    lines = [f"EOD {mode} {session_date}"]
    day_net = 0.0
    for name in sorted(by_strategy):
        s = _trade_stats(by_strategy[name])
        day_net += s["net"]
        db.upsert_strategy_stats(
            name, session_date, mode, trades=s["trades"], wins=s["wins"],
            gross=s["gross"], costs=s["costs"], net=s["net"], avg_r=s["avg_r"],
        )
        pf = s["profit_factor"]
        table.add_row(name, str(s["trades"]), f"{s['win_rate']:.0f}",
                      f"{s['gross']:,.0f}", f"{s['costs']:,.0f}", f"{s['net']:,.0f}",
                      f"{s['avg_r']:.2f}" if s["avg_r"] is not None else "-",
                      f"{pf:.2f}" if pf else "-")
        lines.append(f"  {name}: {s['trades']} trades, net ₹{s['net']:,.0f}")
    console.print(table)
    lines.append(f"Day net: ₹{day_net:,.0f} ({len(trades)} trades)")
    console.print(f"[bold]Day net: ₹{day_net:,.0f}[/bold]")
    return "\n".join(lines)


def promotion_readiness(console: Console | None = None,
                        mode: str = "PAPER") -> list[dict]:
    """Trailing-window per-strategy readiness vs config.PROMOTION_CRITERIA."""
    console = console or Console()
    crit = config.PROMOTION_CRITERIA
    since = (date.today() - timedelta(days=crit["window_sessions"] * 2)).isoformat()
    trades = db.trades_for(mode, since_date=since)
    by_strategy: dict[str, list] = defaultdict(list)
    for t in trades:
        by_strategy[t["strategy"]].append(t)

    results = []
    table = Table(title=f"Promotion readiness ({mode}, since {since})")
    for col in ("Strategy", "Trades", "PF", "Expectancy ₹", "Max DD%", "Worst day%",
                "Verdict", "Failing"):
        table.add_column(col, justify="right")

    for name in sorted(by_strategy):
        ts_list = by_strategy[name]
        s = _trade_stats(ts_list)

        # equity curve over trades for drawdown; daily buckets for worst day
        eq, peak, max_dd = 0.0, 0.0, 0.0
        daily: dict[str, float] = defaultdict(float)
        for t in ts_list:
            eq += t["net_pnl"]
            peak = max(peak, eq)
            base = config.PAPER_STARTING_CASH + peak
            max_dd = max(max_dd, (peak - eq) / base * 100.0)
            daily[t["exit_ts"][:10]] += t["net_pnl"]
        worst_day_pct = min(
            (v / config.PAPER_STARTING_CASH * 100.0 for v in daily.values()),
            default=0.0,
        )

        failing = []
        if s["trades"] < crit["min_trades"]:
            failing.append(f"trades<{crit['min_trades']}")
        if s["profit_factor"] is None or s["profit_factor"] < crit["min_profit_factor"]:
            failing.append(f"PF<{crit['min_profit_factor']}")
        if s["expectancy"] <= crit["min_expectancy_rs"]:
            failing.append("expectancy<=0")
        if max_dd > crit["max_drawdown_pct"]:
            failing.append(f"DD>{crit['max_drawdown_pct']}%")
        if worst_day_pct < crit["worst_day_pct"]:
            failing.append(f"worst day<{crit['worst_day_pct']}%")

        verdict = "READY" if not failing else "NOT READY"
        results.append({"strategy": name, "verdict": verdict, "failing": failing, **s})
        pf = s["profit_factor"]
        table.add_row(
            name, str(s["trades"]),
            f"{pf:.2f}" if pf else "-",
            f"{s['expectancy']:,.0f}", f"{max_dd:.1f}", f"{worst_day_pct:.2f}",
            f"[green]{verdict}[/green]" if verdict == "READY" else f"[yellow]{verdict}[/yellow]",
            ", ".join(failing) or "-",
        )
    console.print(table)
    if not by_strategy:
        console.print("[dim]No paper trades recorded yet.[/dim]")
    return results
