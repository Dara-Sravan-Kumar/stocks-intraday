"""Backtest CLI.

Examples:
  python run_backtest.py --fetch --from 2026-06-15 --to 2026-07-08
  python run_backtest.py --from 2026-06-15 --to 2026-07-08 --strategies orb,gap
  python run_backtest.py --from 2026-06-15 --to 2026-07-08 --symbols RELIANCE,SBIN
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from rich.console import Console
from rich.table import Table

import config
from bot import db, history, instruments
from bot.backtest import run_and_save
from bot.strategies import build_strategies


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay historical 1m bars through the engine")
    ap.add_argument("--from", dest="start", default=None, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="end", default=None, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--symbols", default=None, help="comma list; default = full universe")
    ap.add_argument("--strategies", default=None, help="comma list; default = all enabled")
    ap.add_argument("--fetch", action="store_true",
                    help="backfill 1m history first — from Fyers /history (the "
                         "authorized backtest source; fails loud if unavailable)")
    ap.add_argument("--fetch-dhan", action="store_true",
                    help="backfill via Dhan instead of Fyers (needs a data subscription)")
    ap.add_argument("--fetch-yf", action="store_true",
                    help="DEV ONLY: backfill via yfinance instead of Fyers "
                         "(≤7 days per request, ~30-day lookback)")
    ap.add_argument("--fetch-fyers", action="store_true",
                    help="alias for --fetch (Fyers is the default backtest source)")
    ap.add_argument("--interval", type=int, default=None, metavar="MIN",
                    help="strategy bar interval in minutes (default: config, 5)")
    ap.add_argument("--persist", action="store_true",
                    help="write trades/equity to the DB (mode BACKTEST) so the "
                         "dashboard's Runs tab can show this backtest")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    console = Console()
    if args.interval:
        config.STRATEGY_INTERVAL_MIN = args.interval

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=20)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        symbols = sorted(instruments.load_instruments())
    strat_names = [s.strip() for s in args.strategies.split(",")] if args.strategies else None
    strategies = build_strategies(strat_names)
    console.print(f"[bold]Universe:[/bold] {len(symbols)} symbols | "
                  f"[bold]Strategies:[/bold] {', '.join(s.name for s in strategies)} | "
                  f"[bold]{config.STRATEGY_INTERVAL_MIN}m bars[/bold] | {start} → {end}")

    if args.fetch or args.fetch_dhan or args.fetch_fyers or args.fetch_yf:
        all_syms = symbols + list(config.INDEX_SYMBOLS)
        if args.fetch_dhan:
            console.print("Backfilling 1m history from Dhan…")
            instr = instruments.load_instruments()
            ids = {s: (instr[s].dhan_security_id or "") for s in symbols if s in instr}
            n = history.fetch_1m_dhan(ids, start, end)
        elif args.fetch_yf:
            console.print("[yellow]Backfilling 1m history from yfinance (DEV)…[/yellow]")
            n = history.fetch_1m_yfinance(all_syms, start, end)
        else:
            # Default (and --fetch / --fetch-fyers): the authorized Fyers source.
            console.print("Backfilling 1m history from Fyers /history…")
            try:
                n = history.fetch_1m_fyers(all_syms, start, end)
            except history.FyersHistoryUnavailable as exc:
                console.print(f"[red]Fyers history unavailable:[/red] {exc}")
                console.print("[red]Aborting — not falling back to another source. "
                              "Log in (python -m bot.fyers_auth) or pass --fetch-yf "
                              "for a dev-only yfinance backfill.[/red]")
                return
        console.print(f"stored {n:,} bars; dates in cache: {', '.join(db.bar_dates()[-25:])}")

    result, summary = run_and_save(start=start, end=end, symbols=symbols,
                                   strategies=strategies, persist=args.persist)
    if not result.sessions:
        console.print("[red]No cached bars for that range — run with --fetch first.[/red]")
        return

    table = Table(title=f"Backtest {start} → {end} ({len(result.sessions)} sessions)")
    for col in ("Strategy", "Trades", "Win%", "Gross ₹", "Costs ₹", "Net ₹", "Avg R", "PF"):
        table.add_column(col, justify="right")
    for name, sr in sorted(result.per_strategy.items()):
        pf = result.profit_factor(name)
        table.add_row(
            name, str(sr.trades), f"{sr.win_rate:.0f}",
            f"{sr.gross:,.0f}", f"{sr.costs:,.0f}", f"{sr.net:,.0f}",
            f"{sr.avg_r:.2f}" if sr.avg_r is not None else "-",
            f"{pf:.2f}" if pf not in (None, float('inf')) else ("∞" if pf else "-"),
        )
    console.print(table)

    console.print(
        f"\n[bold]Total net:[/bold] ₹{summary['total_net']:,.0f}  "
        f"[bold]Final equity:[/bold] ₹{summary['final_equity']:,.0f}  "
        f"[bold]Max DD:[/bold] {summary['max_dd_pct']:.1f}%  "
        f"[bold]Green days:[/bold] {summary['green_days']}/{summary['sessions']}"
    )


if __name__ == "__main__":
    main()
