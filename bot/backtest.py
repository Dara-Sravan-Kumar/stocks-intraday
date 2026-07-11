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
                 start: date, end: date, persist: bool = False) -> BacktestResult:
    result = BacktestResult()
    equity = config.PAPER_STARTING_CASH
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
