"""On-demand reporting: EOD summary + promotion-readiness table.

Examples:
  python run_report.py                     # today's paper EOD + readiness
  python run_report.py --date 2026-07-08
  python run_report.py --mode REPLAY
"""
from __future__ import annotations

import argparse

from rich.console import Console

from bot import clock, reports


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=clock.now_ist().date().isoformat())
    ap.add_argument("--mode", default="PAPER", choices=("PAPER", "REPLAY", "BACKTEST", "LIVE"))
    args = ap.parse_args()

    console = Console()
    reports.eod_report(args.mode, args.date, console)
    reports.promotion_readiness(console, mode=args.mode)


if __name__ == "__main__":
    main()
