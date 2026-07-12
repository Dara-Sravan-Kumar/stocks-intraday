"""Event-driven backtest: replay cached 1m bars through the real Engine,
one session per day, fresh book each day, results aggregated per strategy."""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

import config
from bot import clock, db, history
from bot.engine import Engine
from bot.execution.paper_broker import PaperBroker
from bot.feeds.replay_feed import ReplayFeed
from bot.risk import RiskEngine
from bot.state import MarketState
from bot.strategies import Strategy

log = logging.getLogger(__name__)


@dataclass
class StrategyResult:
    trades: int = 0
    wins: int = 0
    gross: float = 0.0
    costs: float = 0.0
    net: float = 0.0
    r_sum: float = 0.0
    r_count: int = 0
    by_reason: dict = field(default_factory=lambda: defaultdict(int))

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0

    @property
    def profit_factor(self) -> float | None:
        # computed at report time from trade list; kept simple here
        return None

    @property
    def avg_r(self) -> float | None:
        return self.r_sum / self.r_count if self.r_count else None


@dataclass
class BacktestResult:
    sessions: list[date] = field(default_factory=list)
    daily_pnl: dict = field(default_factory=dict)          # date -> net
    per_strategy: dict = field(default_factory=lambda: defaultdict(StrategyResult))
    all_trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)       # (date, equity)

    def profit_factor(self, strategy: str | None = None) -> float | None:
        trades = [t for t in self.all_trades
                  if strategy is None or t.position.strategy == strategy]
        gains = sum(t.net_pnl for t in trades if t.net_pnl > 0)
        losses = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
        if losses == 0:
            return None if gains == 0 else float("inf")
        return gains / losses


def run_backtest(symbols: list[str], strategies: list[Strategy],
                 start: date, end: date, persist: bool = False,
                 starting_cash: float | None = None) -> BacktestResult:
    result = BacktestResult()
    equity = starting_cash if starting_cash is not None else config.PAPER_STARTING_CASH
    available = set(db.bar_dates())   # once — a full-table scan per session leaks memory
    d = start
    while d <= end:
        if not clock.is_trading_day(d) or d.isoformat() not in available:
            d += timedelta(days=1)
            continue

        prev_levels = history.build_prev_day_levels(symbols, d)
        market = MarketState(symbols, prev_levels)
        feed = ReplayFeed(
            symbols + list(config.INDEX_SYMBOLS),
            f"{d.isoformat()}T00:00", f"{d.isoformat()}T23:59",
        )
        broker = PaperBroker(equity)   # compounding across sessions
        engine = Engine(
            mode="BACKTEST", feed=feed, broker=broker,
            strategies=strategies, risk=RiskEngine(),
            market=market, persist=persist,
        )
        engine.run()

        day_net = sum(t.net_pnl for t in engine.closed_trades)
        equity = broker.equity({})
        result.sessions.append(d)
        result.daily_pnl[d] = day_net
        result.equity_curve.append((d, equity))
        result.all_trades.extend(engine.closed_trades)
        for t in engine.closed_trades:
            sr = result.per_strategy[t.position.strategy]
            sr.trades += 1
            sr.wins += 1 if t.net_pnl > 0 else 0
            sr.gross += t.gross_pnl
            sr.costs += t.costs
            sr.net += t.net_pnl
            if t.r_multiple is not None:
                sr.r_sum += t.r_multiple
                sr.r_count += 1
            sr.by_reason[t.exit_reason] += 1
        log.info("backtest %s: %d trades, net %.0f, equity %.0f",
                 d, len(engine.closed_trades), day_net, equity)
        d += timedelta(days=1)
    return result


def max_drawdown_pct(equity_curve: list[tuple]) -> float:
    peak, max_dd = float("-inf"), 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak * 100.0)
    return max_dd


# --- shared on-demand runner (CLI + dashboard button) -----------------------

#: period label -> (sessions to replay, history depth in days for an optional
#: fetch). The on-demand backtest can pull MORE history than the light daily
#: runs; depths are chosen to stay within what Fyers can actually return.
PERIODS: dict[str, tuple[int, int]] = {
    "1 week": (5, 10),
    "1 month": (22, 45),
    "3 months": (66, 130),
    "6 months": (127, 260),
}


def summarize(result: BacktestResult, starting_cash: float | None = None) -> dict:
    start_cash = starting_cash if starting_cash is not None else config.PAPER_STARTING_CASH
    total_net = sum(result.daily_pnl.values())
    final_eq = result.equity_curve[-1][1] if result.equity_curve else start_cash
    green = sum(1 for v in result.daily_pnl.values() if v > 0)
    return {
        "sessions": len(result.sessions),
        "trades": len(result.all_trades),
        "total_net": total_net,
        "final_equity": final_eq,
        "max_dd_pct": max_drawdown_pct(result.equity_curve),
        "green_days": green,
        "profit_factor": result.profit_factor(),
    }


def _seed_strategies(channel: str = "DISCOVERED_EQ") -> list[Strategy]:
    """The SEED_GENES library as a live ExprStrategy, for a seeds-only backtest
    (equity only — index-option premium history isn't backtestable)."""
    from bot.discovery.mixer import SEED_GENES
    from bot.discovery.spec import StrategySpec
    from bot.strategies.discovered import DiscoveredEquity

    specs = [StrategySpec(name=f"seed_{i}", entry_expr=expr, channel="DISCOVERED_EQ",
                          side=side, min_reward_risk=rr, source="manual")
             for i, (expr, side, rr) in enumerate(SEED_GENES["DISCOVERED_EQ"])]
    return [DiscoveredEquity(specs)]


def run_and_save(*, period: str | None = None, start: date | None = None,
                 end: date | None = None, symbols: list[str] | None = None,
                 strategies: list[Strategy] | None = None, seeds_only: bool = False,
                 max_instruments: int | None = None,
                 starting_cash: float | None = None, persist: bool = False,
                 fetch: bool = False, fetch_source: str = "fyers") -> tuple[BacktestResult, dict]:
    """One entry point for a backtest run. Resolves a period (or explicit
    start/end) against the cached bars, optionally fetching a deeper history
    window, builds the fleet (or a seeds-only fleet), runs the replay, and
    returns (result, summary). The DB is only written when persist/fetch is set."""
    from bot.strategies import build_strategies

    sessions, depth = PERIODS.get(period or "", (22, 45))
    if symbols is None:
        symbols = sorted(db.bar_symbols(exclude=set(config.INDEX_SYMBOLS)))
    if max_instruments:
        symbols = symbols[:max_instruments]

    if fetch and (start is None or end is None):
        end_guess = date.fromisoformat(db.bar_dates()[-1]) if db.bar_dates() else date.today()
        _fetch(symbols, end_guess - timedelta(days=depth), end_guess, fetch_source)

    dates = db.bar_dates()
    if not dates:
        return BacktestResult(), {"error": "no cached bars — fetch history first"}
    if start is None or end is None:
        end = date.fromisoformat(dates[-1])
        start = date.fromisoformat(dates[-sessions:][0])

    if strategies is None:
        strategies = _seed_strategies() if seeds_only else build_strategies()
    result = run_backtest(symbols, strategies, start, end,
                          persist=persist, starting_cash=starting_cash)
    return result, summarize(result, starting_cash)


def _fetch(symbols: list[str], start: date, end: date, source: str) -> int:
    from bot import history, instruments
    all_syms = symbols + list(config.INDEX_SYMBOLS)
    if source == "fyers":
        return history.fetch_1m_fyers(all_syms, start, end)
    if source == "dhan":
        instr = instruments.load_instruments()
        ids = {s: (instr[s].dhan_security_id or "") for s in symbols if s in instr}
        return history.fetch_1m_dhan(ids, start, end)
    return history.fetch_1m_yfinance(all_syms, start, end)
