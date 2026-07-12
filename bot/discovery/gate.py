"""The backtest gate — the overfitting defense for discovered/bred specs.

Replay a spec's entry_expr over cached 1m bars, split chronologically into an
IN-SAMPLE (older) and OUT-OF-SAMPLE (recent) window, and pass ONLY if the OOS
window clears trade-count / win-rate / profit-factor floors AND the in-sample
window is net-positive. The recent OOS window is the real test: it's the data
the spec was never fitted on.

Two instrument modes, both replayed on cached bars (option premium history is
unavailable on free data, so options are gated by a PROXY on the underlying):
  * DISCOVERED_EQ  — replay on the stock's own bars; a real intraday round trip.
  * DISCOVERED_OPT — replay on the INDEX bars; the entry_expr fires on index
    structure and we score the index move in the signal's direction as a proxy
    for the ATM CE/PE the live channel would buy. Heavier friction, tighter stop.

Everything squares off at session end — no position survives the day.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import config
from bot.bars import Bar
from bot.clock import IST, parse_hhmm
from bot.discovery.expr import CompiledExpr
from bot.discovery.expr import eval_expr
from bot.discovery.spec import StrategySpec, validate_spec
from bot.discovery.vocab import build_snapshot
from bot.indicators import PrevDayLevels
from bot.state import SymbolState

log = logging.getLogger(__name__)

MIN_BARS = 2   # 5m bars before a spec may fire (mirrors config.MIN_BARS_FOR_SIGNALS)


@dataclass
class GateResult:
    passed: bool
    reason: str
    total_trades: int = 0
    is_trades: int = 0
    is_net_pct: float = 0.0
    oos_trades: int = 0
    oos_win_rate: float = 0.0
    oos_profit_factor: float | None = None
    oos_net_pct: float = 0.0

    def to_dict(self) -> dict:
        return {
            "passed": self.passed, "reason": self.reason,
            "total_trades": self.total_trades,
            "is_trades": self.is_trades, "is_net_pct": round(self.is_net_pct, 3),
            "oos_trades": self.oos_trades,
            "oos_win_rate": round(self.oos_win_rate, 1),
            "oos_profit_factor": (None if self.oos_profit_factor is None
                                  else round(self.oos_profit_factor, 3)),
            "oos_net_pct": round(self.oos_net_pct, 3),
        }


@dataclass
class _Trade:
    day: date
    pnl_pct: float


@dataclass
class _Session:
    day: date
    bars: list[Bar]
    prev_day: PrevDayLevels = field(default_factory=PrevDayLevels)


# --- replay -----------------------------------------------------------------

def _replay_session(spec: StrategySpec, compiled: CompiledExpr, sess: _Session,
                    *, is_option: bool) -> list[_Trade]:
    cfg = config.BACKTEST_GATE
    st = SymbolState(sess.day.isoformat(), sess.prev_day)
    deadline = parse_hhmm(cfg["entry_deadline"], sess.day)
    stop_pct = cfg["opt_stop_pct"] if is_option else cfg["eq_stop_pct"]
    friction = cfg["opt_friction_pct"] if is_option else cfg["friction_pct"]
    target_pct = stop_pct * spec.min_reward_risk
    long_dir = spec.side == "LONG"

    trades: list[_Trade] = []
    pos_entry: float | None = None
    pos_bars = 0
    stop_lvl = target_lvl = 0.0

    for bar in sess.bars:
        if pos_entry is not None:
            pos_bars_completed = pos_bars   # 5m bars elapsed since entry
            exit_px = None
            if long_dir:
                if bar.low <= stop_lvl:
                    exit_px = stop_lvl
                elif bar.high >= target_lvl:
                    exit_px = target_lvl
            else:
                if bar.high >= stop_lvl:
                    exit_px = stop_lvl
                elif bar.low <= target_lvl:
                    exit_px = target_lvl
            if exit_px is None and pos_bars_completed >= cfg["hold_bars_max"]:
                exit_px = bar.close   # intraday time-exit
            if exit_px is not None:
                move = (exit_px - pos_entry) / pos_entry * 100.0
                pnl = (move if long_dir else -move) - friction
                trades.append(_Trade(sess.day, pnl))
                pos_entry = None

        done_5m = st.on_bar_1m(bar)
        if done_5m is not None and pos_entry is not None:
            pos_bars += 1

        if (pos_entry is None and done_5m is not None
                and len(st.bars_5m) >= MIN_BARS
                and bar.ts < deadline):
            snap = build_snapshot(st)
            if eval_expr(compiled, snap.as_env()):
                pos_entry = bar.close
                pos_bars = 0
                if long_dir:
                    stop_lvl = pos_entry * (1 - stop_pct / 100.0)
                    target_lvl = pos_entry * (1 + target_pct / 100.0)
                else:
                    stop_lvl = pos_entry * (1 + stop_pct / 100.0)
                    target_lvl = pos_entry * (1 - target_pct / 100.0)

    if pos_entry is not None and sess.bars:   # forced square-off
        move = (sess.bars[-1].close - pos_entry) / pos_entry * 100.0
        pnl = (move if long_dir else -move) - friction
        trades.append(_Trade(sess.day, pnl))
    return trades


def _split_stats(trades: list[_Trade]) -> tuple[int, float, float, float | None]:
    n = len(trades)
    net = sum(t.pnl_pct for t in trades)
    wins = sum(1 for t in trades if t.pnl_pct > 0)
    gains = sum(t.pnl_pct for t in trades if t.pnl_pct > 0)
    losses = -sum(t.pnl_pct for t in trades if t.pnl_pct < 0)
    win_rate = wins / n * 100.0 if n else 0.0
    pf = None if losses == 0 else gains / losses
    return n, net, win_rate, pf


def backtest_gate(spec: StrategySpec,
                  histories: dict[str, list[_Session]] | None = None) -> GateResult:
    """Gate a spec. `histories` maps instrument symbol -> chronologically sorted
    list of _Session; if omitted it is loaded from the DB via load_gate_histories.
    Sessions are pooled across the spec's instruments, then split IS/OOS."""
    cfg = config.BACKTEST_GATE
    try:
        compiled = validate_spec(spec)
    except Exception as exc:  # noqa: BLE001
        return GateResult(False, f"invalid spec: {exc}")

    if histories is None:
        histories = load_gate_histories(spec)
    is_option = spec.channel == "DISCOVERED_OPT"

    # Determine the OOS cutoff from the union of session dates across instruments.
    all_days = sorted({s.day for sessions in histories.values() for s in sessions})
    if len(all_days) < 4:
        return GateResult(False, f"insufficient history ({len(all_days)} sessions)")
    split_at = int(len(all_days) * (1 - cfg["oos_fraction"]))
    split_at = max(1, min(split_at, len(all_days) - 1))
    oos_days = set(all_days[split_at:])

    all_trades: list[_Trade] = []
    for sessions in histories.values():
        for sess in sessions:
            all_trades.extend(_replay_session(spec, compiled, sess, is_option=is_option))

    is_trades = [t for t in all_trades if t.day not in oos_days]
    oos_trades = [t for t in all_trades if t.day in oos_days]
    is_n, is_net, _, _ = _split_stats(is_trades)
    oos_n, oos_net, oos_wr, oos_pf = _split_stats(oos_trades)

    res = GateResult(
        passed=False, reason="", total_trades=len(all_trades),
        is_trades=is_n, is_net_pct=is_net,
        oos_trades=oos_n, oos_win_rate=oos_wr, oos_profit_factor=oos_pf,
        oos_net_pct=oos_net,
    )

    if oos_n < cfg["min_oos_trades"]:
        res.reason = f"OOS trades {oos_n} < {cfg['min_oos_trades']}"
        return res
    if is_net <= 0:
        res.reason = f"in-sample net {is_net:.2f}% not positive"
        return res
    if oos_wr < cfg["min_win_rate"]:
        res.reason = f"OOS win-rate {oos_wr:.0f}% < {cfg['min_win_rate']:.0f}%"
        return res
    pf_val = float("inf") if oos_pf is None else oos_pf
    if pf_val < cfg["min_profit_factor"]:
        res.reason = f"OOS PF {pf_val:.2f} < {cfg['min_profit_factor']:.2f}"
        return res

    res.passed = True
    res.reason = (f"OOS {oos_n} trades, {oos_wr:.0f}% win, "
                  f"PF {pf_val:.2f}, IS net {is_net:+.1f}%")
    return res


# --- production history loader ----------------------------------------------

def _gate_symbols(spec: StrategySpec) -> list[str]:
    from bot import db
    if spec.channel == "DISCOVERED_OPT":
        return [spec.underlying or "NIFTY"]
    stocks = db.bar_symbols(exclude=set(config.INDEX_SYMBOLS))
    return stocks[: config.DISCOVERED_EQ_MAX_SYMBOLS]


def load_gate_histories(spec: StrategySpec,
                        sessions: int | None = None) -> dict[str, list[_Session]]:
    """Load the most-recent N sessions of 1m bars per instrument from the DB."""
    from bot import db, history

    sessions = sessions or config.BACKTEST_GATE["gate_sessions"]
    symbols = _gate_symbols(spec)
    out: dict[str, list[_Session]] = {}
    for sym in symbols:
        all_dates = db.bar_dates(sym)[-sessions:]
        if not all_dates:
            continue
        start_ts, end_ts = all_dates[0] + "T00:00", all_dates[-1] + "T23:59"
        rows = db.load_bars([sym], start_ts, end_ts)
        by_day: dict[str, list[Bar]] = {}
        for r in rows:
            by_day.setdefault(r["ts"][:10], []).append(
                Bar(sym, datetime.fromisoformat(r["ts"]), r["open"], r["high"],
                    r["low"], r["close"], r["volume"]))
        sess_list: list[_Session] = []
        for d in all_dates:
            bars = by_day.get(d)
            if not bars:
                continue
            day = date.fromisoformat(d)
            prev = history.build_prev_day_levels([sym], day).get(sym, PrevDayLevels())
            sess_list.append(_Session(day, bars, prev))
        if sess_list:
            out[sym] = sess_list
    return out
