"""Run a trading session (paper by default).

Examples:
  python run_live.py                          # paper session, auto feed (dhan if token, else yfinance)
  python run_live.py --feed yf                # force the free yfinance 1m feed
  python run_live.py --replay 2026-07-08      # off-hours dry run from cached bars (mode=REPLAY)
  python run_live.py --live                   # LIVE orders — only with ALL gates satisfied

Live gating (all required): config.LIVE_TRADING_ENABLED, strategy in
config.LIVE_STRATEGY_ALLOWLIST, .env DHAN_LIVE_CONFIRM, and this --live flag.
Non-allowlisted strategies keep paper-trading in the same session.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from rich.console import Console

import config
from bot import alerts, clock, db, history, instruments, reports
from bot.dashboard import Dashboard
from bot.engine import Engine
from bot.execution.paper_broker import PaperBroker
from bot.feeds.replay_feed import ReplayFeed
from bot.feeds.yf_feed import YfFeed
from bot.risk import RiskEngine
from bot.state import MarketState
from bot.strategies import build_strategies

log = logging.getLogger(__name__)


def setup_logging(session_date: str) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(config.LOG_DIR / f"{session_date}.log", encoding="utf-8"),
        ],
    )


def make_feed(pref: str, symbols: list[str], instr: dict, allow_degrade: bool = True):
    from bot import fyers_auth
    if pref in ("auto", "fyers") and fyers_auth.has_credentials():
        try:
            from bot.feeds.fyers_feed import FyersFeed
            # self-degrades to yfinance if token invalid (unless allow_degrade
            # is False, e.g. --live, where a feed failure aborts instead)
            return FyersFeed(symbols, allow_degrade=allow_degrade)
        except Exception as exc:  # noqa: BLE001
            log.warning("Fyers feed unavailable (%s); trying next feed", exc)
    if pref == "fyers":
        raise SystemExit("--feed fyers requires FYERS_APP_ID and FYERS_SECRET_ID in .env")
    if pref in ("auto", "dhan"):
        s = config.dhan_settings()
        if s["client_id"] and s["access_token"]:
            try:
                from bot.feeds.dhan_feed import DhanFeed
                return DhanFeed(symbols, instr)
            except Exception as exc:  # noqa: BLE001
                log.warning("Dhan feed unavailable (%s); falling back to yfinance", exc)
        elif pref == "dhan":
            raise SystemExit("--feed dhan requires DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env")
    return YfFeed(symbols)


def make_broker(live_flag: bool, console: Console):
    """Paper book seeded from the persistent ledger; live wrapper only when gated."""
    from bot import fyers_auth
    paper_start = config.PAPER_STARTING_CASH + db.realized_net_pnl("PAPER")
    paper = PaperBroker(paper_start)
    if not live_flag:
        return paper
    from bot.execution.dhan_broker import HybridBroker, LiveTradingBlocked
    try:
        if fyers_auth.has_credentials():
            from bot.execution.fyers_broker import FyersHybridBroker
            return FyersHybridBroker(paper)
        return HybridBroker(paper)
    except LiveTradingBlocked as exc:
        console.print(f"[red]LIVE blocked:[/red] {exc} — continuing in PAPER mode.")
        return paper


def abandon_stale_positions(mode: str) -> None:
    """Positions left OPEN by a crashed run can't be trusted — mark them."""
    for row in db.open_positions(mode):
        db.close_position(row["id"], clock.now_ist().isoformat())
        db.log_skip(clock.now_ist().isoformat(), mode, row["strategy"], row["symbol"],
                    "stale OPEN position from previous run marked closed")


def main() -> None:
    ap = argparse.ArgumentParser(description="Intraday bot session")
    ap.add_argument("--feed", choices=("auto", "fyers", "dhan", "yf"),
                    default=config.FEED_PREFERENCE)
    ap.add_argument("--options", action="store_true",
                    help="trade NIFTY/BANKNIFTY index options (mode PAPER-OPT; "
                         "requires the Fyers feed)")
    ap.add_argument("--replay", metavar="YYYY-MM-DD", default=None,
                    help="replay a cached session instead of live data")
    ap.add_argument("--live", action="store_true", help="enable gated live order routing")
    ap.add_argument("--symbols", default=None, help="comma list; default = universe")
    ap.add_argument("--strategies", default=None, help="comma list; default = all enabled")
    ap.add_argument("--no-dashboard", action="store_true")
    ap.add_argument("--no-warmup", action="store_true", help="skip history backfill")
    ap.add_argument("--refresh-universe", action="store_true")
    args = ap.parse_args()

    console = Console()
    replay_mode = args.replay is not None
    if replay_mode:
        session_date = date.fromisoformat(args.replay)
        mode = "REPLAY"
    else:
        session_date = clock.now_ist().date()
        # engine mode label; live routing happens inside HybridBroker
        mode = "PAPER-OPT" if args.options else "PAPER"
        if not clock.is_trading_day(session_date):
            console.print(f"[yellow]{session_date} is not a trading day — exiting.[/yellow]")
            return

    setup_logging(session_date.isoformat())
    strat_names = [s.strip() for s in args.strategies.split(",")] if args.strategies else None
    strategies = build_strategies(strat_names, options_mode=args.options)

    option_contracts: dict = {}
    if args.options:
        from bot import options as optmod
        instr = {}
        index_names = list(config.INDEX_SYMBOLS)
        all_contracts = optmod.load_contracts()
        symbols = list(index_names)          # indices are full SymbolStates here
        for u in config.OPTIONS["underlyings"]:
            spot = optmod.spot_price(u)
            if spot is None:
                console.print(f"[red]No spot for {u} — is the Fyers token valid?[/red]")
                continue
            chain = optmod.build_chain(u, spot, session_date, all_contracts)
            for c in chain:
                option_contracts[c.symbol] = c
                symbols.append(c.symbol)
            if chain:
                console.print(f"{u}: spot {spot:,.0f}, expiry {chain[0].expiry}, "
                              f"{len(chain)} contracts, lot {chain[0].lot}")
        if not option_contracts:
            console.print("[red]No option chains available — aborting.[/red]")
            return
    else:
        instr = instruments.load_instruments(refresh=args.refresh_universe)
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",")]
        else:
            symbols = sorted(instr)
    console.print(f"[bold]{mode}[/bold] session {session_date} | {len(symbols)} symbols | "
                  f"strategies: {', '.join(s.name for s in strategies)}")

    if not replay_mode and not args.no_warmup and not args.options:
        console.print("Warming up 1m history (prev-day levels, RVOL profiles)…")
        try:
            history.fetch_1m_yfinance(
                symbols + list(config.INDEX_SYMBOLS),
                session_date - timedelta(days=config.WARMUP_DAYS),
                session_date - timedelta(days=1),
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]warmup failed ({exc}) — continuing without[/yellow]")

    prev_levels = history.build_prev_day_levels(
        [s for s in symbols if not s.startswith("NSE:")], session_date)
    market = MarketState(symbols, prev_levels, option_contracts=option_contracts)
    market.session_date = session_date.isoformat()

    if replay_mode:
        feed = ReplayFeed(symbols + list(config.INDEX_SYMBOLS),
                          f"{session_date}T00:00", f"{session_date}T23:59")
    elif args.options:
        from bot.feeds.fyers_feed import FyersFeed
        # options have no yfinance equivalent: abort rather than degrade
        feed = FyersFeed(symbols, allow_degrade=False)
    else:
        feed = make_feed(args.feed, symbols, instr, allow_degrade=not args.live)

    abandon_stale_positions(mode)
    if args.options:
        paper_start = config.OPTIONS["paper_capital"] + db.realized_net_pnl(mode)
        broker = PaperBroker(paper_start)    # options live trading not wired — paper only
    else:
        broker = make_broker(args.live and not replay_mode, console)
    # Production paper policy: book ONLY on the real Fyers feed. A yfinance
    # fallback/degrade freezes the book (scan/log/alert only). The intentional
    # --feed yf (or --feed dhan) dev workflow and replays are exempt.
    require_fyers_feed = (not replay_mode) and args.feed in ("auto", "fyers")
    engine = Engine(
        mode=mode, feed=feed, broker=broker, strategies=strategies,
        risk=RiskEngine(), market=market, persist=True,
        idle_sleep=0.0 if replay_mode else 2.0,
        require_fyers_feed=require_fyers_feed,
    )

    dash = None
    if not args.no_dashboard and sys.stdout.isatty():
        dash = Dashboard(engine)
        engine.on_event = dash.on_event
        dash.run_in_background()

    try:
        engine.run()
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted — squaring off open positions.[/yellow]")
        engine._square_off_all("SQUAREOFF")  # noqa: SLF001
    finally:
        if dash:
            dash.stop()

    summary = reports.eod_report(mode, session_date.isoformat(), console)
    reports.promotion_readiness(console)
    if not replay_mode:
        alerts.send(f"**{session_date}** session done\n```{summary}```")
        # Tail of the run: discover + breed once/day (expensive; LLM + backtests).
        # Fully guarded — a discovery failure must never affect the trading run.
        try:
            from bot.discovery.automation import run_daily_discovery
            rep = run_daily_discovery()
            if "skipped" not in rep:
                console.print(f"[dim]Daily discovery: "
                              f"{'; '.join(f'{c}={v}' for c, v in rep['channels'].items())}[/dim]")
        except Exception as exc:  # noqa: BLE001
            log.warning("daily discovery failed: %s", exc)


if __name__ == "__main__":
    main()
