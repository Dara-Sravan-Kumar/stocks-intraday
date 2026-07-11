"""The one event loop. Backtest, paper, and live differ only in the feed,
the broker, and how time advances (bar timestamps drive 'now').

Per minute batch of completed 1m bars:
  1. route index bars to MarketState
  2. fill pending entries at this bar's open
  3. check stop/target exits against this bar (pre-trail stops — no look-ahead)
  4. update symbol state (1m -> indicators -> maybe a 5m bar)
  5. strategy.manage() for open positions (trails, time stops)
  6. on completed 5m bars: collect signals -> risk -> queue pending entries
  7. square-off / daily-loss / circuit-breaker checks
  8. equity mark
"""
from __future__ import annotations

import logging
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, timedelta

import config
from bot import clock, db
from bot.bars import Bar
from bot.execution import Broker, ClosedTrade, Position
from bot.feeds import Feed
from bot.risk import Approval, DayState, RiskEngine, Skip
from bot.state import MarketState
from bot.strategies import Signal, Strategy

log = logging.getLogger(__name__)

INDEX_NAMES = set(config.INDEX_SYMBOLS)


@dataclass
class PendingEntry:
    signal: Signal
    approval: Approval
    queued_ts: datetime


class Engine:
    def __init__(self, *, mode: str, feed: Feed, broker: Broker,
                 strategies: list[Strategy], risk: RiskEngine,
                 market: MarketState, persist: bool = True,
                 idle_sleep: float = 1.0,
                 on_event=None):
        self.mode = mode
        self.feed = feed
        self.broker = broker
        self.strategies = strategies
        self.risk = risk
        self.market = market
        self.persist = persist
        self.idle_sleep = idle_sleep
        self.on_event = on_event or (lambda kind, msg: None)

        self.now: datetime | None = None
        self.marks: dict[str, float] = {}
        self.pending: list[PendingEntry] = []
        self.day: DayState | None = None
        self.run_id: int | None = None
        self.bars_processed = 0
        self.n_signals = 0
        self.n_trades = 0
        self.closed_trades: list[ClosedTrade] = []
        self.warnings: list[str] = []
        self._squared_off = False
        self._strategy_by_name = {s.name: s for s in strategies}

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        self.feed.start()
        start_equity = self.broker.equity({})
        self.day = DayState(start_equity=start_equity)
        for s in self.strategies:
            s.on_session_start()
        if self.persist:
            self.run_id = db.start_run(
                self.mode, datetime.now().date().isoformat(),
                self.feed.source_name,
                datetime.now().isoformat(timespec="seconds"),
            )
        self._event("run", f"{self.mode} session started, equity ₹{start_equity:,.0f}")
        try:
            while True:
                batch = self.feed.poll()
                if not batch:
                    if self.feed.exhausted:
                        break
                    if self._live_should_stop():
                        break
                    time_mod.sleep(self.idle_sleep)
                    continue
                self.process_minute(batch)
        finally:
            self._finalize()

    def _live_should_stop(self) -> bool:
        """In live/paper mode with a real-time feed, stop after session close
        (and square off on wall clock even if bars stall)."""
        if self.feed.source_name == "replay":
            return False
        now = clock.now_ist()
        ph = clock.phase(now)
        if ph == clock.SQUAREOFF and not self._squared_off:
            self._square_off_all("SQUAREOFF (wall clock)")
        return ph == clock.CLOSED and now.time() > clock.parse_hhmm(
            config.SESSION["market_close"], now.date()).time()

    # ---------------------------------------------------------- minute step

    def process_minute(self, batch: list[Bar]) -> None:
        batch = sorted(batch, key=lambda b: (b.ts, b.symbol))
        self.now = max(b.ts for b in batch) + timedelta(minutes=1)  # bar end
        phase = clock.phase(self.now)

        completed_5m: list[str] = []
        for bar in batch:
            if bar.symbol in INDEX_NAMES:
                self.market.on_index_tick(bar.symbol, bar.ts, bar.close)
                # In options mode the index is also a full SymbolState that
                # strategies read; otherwise it's tracker-only.
            st = self.market.get(bar.symbol)
            if st is None:
                continue
            self.bars_processed += 1
            self.marks[bar.symbol] = bar.close

            self._fill_pending(bar, phase)
            self._check_price_exits(bar)
            if st.on_bar_1m(bar) is not None:
                completed_5m.append(bar.symbol)
            self._manage_positions(bar)

        if phase == clock.SQUAREOFF and not self._squared_off:
            self._square_off_all("SQUAREOFF")
        elif not self.day.halted:
            equity = self.broker.equity(self.marks)
            if self.risk.daily_loss_breached(equity, self.day):
                self.day.halted = True
                self.day.halt_reason = "max daily loss"
                self._event("halt", f"MAX DAILY LOSS hit at ₹{equity:,.0f} — flat for the day")
                self._close_all("HALT")

        cb = self.risk.check_circuit_breaker(self.market, self.day, self.now)
        if cb:
            self._event("halt", cb)
            self._log_skip(None, None, cb)

        # drop pendings whose instrument produced no bar within the window
        for p in list(self.pending):
            if self.now - p.queued_ts > timedelta(minutes=3):
                self.pending.remove(p)
                self._log_skip(p.signal.strategy, p.signal.symbol,
                               "entry dropped: no fill bar within 3 min")

        if completed_5m and not self._squared_off and not self.day.halted:
            self._collect_signals(completed_5m)

        self._mark_equity()
        self._heartbeat()

    # ------------------------------------------------------------- entries

    def _collect_signals(self, symbols: list[str]) -> None:
        if not clock.entries_allowed(self.now):
            return
        # Phase 1: gather every signal this minute across symbols/strategies.
        candidates: list[tuple[float, Signal]] = []   # (rvol_score, signal)
        for sym in symbols:
            st = self.market.get(sym)
            if len(st.bars_5m) < config.MIN_BARS_FOR_SIGNALS:
                continue
            for strat in self.strategies:
                try:
                    result = strat.on_bar_5m(st, self.market, self.now)
                except Exception as exc:  # noqa: BLE001 — one bad strategy never kills the loop
                    self._warn(f"{strat.name} on_bar_5m({sym}) raised: {exc}")
                    continue
                sigs = result if isinstance(result, list) else ([result] if result else [])
                for sig in sigs:
                    self.n_signals += 1
                    if any(p.signal.symbol == sig.symbol for p in self.pending):
                        self._log_skip(sig.strategy, sig.symbol, "entry already pending")
                        continue
                    if sig.symbol == sym and not self.risk.regime_allows(sig.side, self.market):
                        # regime filter applies to directional equity/index trades,
                        # not multi-leg option structures on another instrument
                        self._log_skip(sig.strategy, sym,
                                       f"regime filter: NIFTY against {sig.side}")
                        continue
                    candidates.append((st.ind.rvol() or 0.0, sig))

        # Phase 2: strongest activity first — the day's entry budget goes to
        # the highest-conviction signals, not the alphabetically first ones.
        candidates.sort(key=lambda c: c[0], reverse=True)
        for _, sig in candidates:
            if any(p.signal.symbol == sig.symbol for p in self.pending):
                self._log_skip(sig.strategy, sig.symbol, "entry already pending")
                continue
            st = self.market.get(sig.symbol)
            if st is None or not (st.bars_5m or st.bars_1m):
                self._log_skip(sig.strategy, sig.symbol, "no market data for instrument")
                continue
            ref_price = st.bars_5m[-1].close if st.bars_5m else st.bars_1m[-1].close
            pending_margin = sum(p.approval.margin for p in self.pending)
            res = self.risk.approve(
                strategy=sig.strategy, symbol=sig.symbol,
                entry_price=ref_price, stop_price=sig.stop,
                sym_state=st, open_positions=self.broker.positions,
                equity=self.broker.equity(self.marks),
                margin_used=self.broker.margin_used + pending_margin,
                day=self.day, now=self.now, side=sig.side,
            )
            if isinstance(res, Skip):
                self._log_skip(sig.strategy, sig.symbol, res.reason)
                continue
            self.day.entries_today += 1
            self.pending.append(PendingEntry(sig, res, self.now))
            self._event("signal", f"{sig.strategy} {sig.side} {sig.symbol}: {sig.reason} "
                                  f"(qty {res.qty})")

    def _fill_pending(self, bar: Bar, phase: str) -> None:
        mine = [p for p in self.pending if p.signal.symbol == bar.symbol]
        for p in mine:
            self.pending.remove(p)
            if self.day.halted or phase not in (clock.OPEN,):
                self._log_skip(p.signal.strategy, bar.symbol, "entry dropped: phase/halt")
                continue
            if bar.ts + timedelta(minutes=1) - p.queued_ts > timedelta(minutes=3):
                self._log_skip(p.signal.strategy, bar.symbol, "entry dropped: stale")
                continue
            sig, ap = p.signal, p.approval
            st = self.market.get(bar.symbol)
            instrument = "OPT" if (st and st.option_meta) else "EQ"
            pos = self.broker.open_position(
                sig.strategy, sig.symbol, sig.side, ap.qty,
                ref_price=bar.open, ts=bar.ts, stop=sig.stop, target=sig.target,
                margin=ap.margin, instrument=instrument,
            )
            if pos is None:
                self._log_skip(sig.strategy, sig.symbol, "broker rejected entry")
                continue
            pos.mode = self.mode
            strat = self._strategy_by_name.get(sig.strategy)
            if strat:
                strat.note_entry(sig.symbol, sig.side)
            if self.persist:
                pos.db_id = db.open_position(
                    run_id=self.run_id, mode=self.mode, strategy=sig.strategy,
                    symbol=sig.symbol, side=sig.side, qty=ap.qty,
                    entry_ts=pos.entry_ts.isoformat(), entry_price=pos.entry_price,
                    stop_price=pos.stop_price, target_price=pos.target_price,
                    margin_used=pos.margin_used,
                )
            self._event("entry", f"{sig.strategy} {sig.side} {sig.symbol} x{ap.qty} "
                                 f"@ {pos.entry_price:.2f} stop {pos.stop_price:.2f}")

    # --------------------------------------------------------------- exits

    def _positions_for(self, symbol: str) -> list[Position]:
        return [p for p in self.broker.positions if p.symbol == symbol]

    def _check_price_exits(self, bar: Bar) -> None:
        from bot.execution.paper_broker import exit_fill_price, stop_hit, target_hit
        for pos in self._positions_for(bar.symbol):
            if pos.entry_ts >= bar.ts:
                continue  # opened this bar; evaluate from the next bar
            if stop_hit(pos, bar):
                price = exit_fill_price(pos, bar, pos.stop_price)
                self._close(pos, price, bar.ts, "STOP")
            elif target_hit(pos, bar):
                price = exit_fill_price(pos, bar, pos.target_price)
                self._close(pos, price, bar.ts, "TARGET")

    def _manage_positions(self, bar: Bar) -> None:
        st = self.market.get(bar.symbol)
        for pos in self._positions_for(bar.symbol):
            strat = self._strategy_by_name.get(pos.strategy)
            if strat is None:
                continue
            try:
                req = strat.manage(pos, st, self.now)
            except Exception as exc:  # noqa: BLE001
                self._warn(f"{pos.strategy} manage({pos.symbol}) raised: {exc}")
                continue
            if req is not None:
                self._close(pos, bar.close, bar.ts, req.reason)
            elif self.persist and pos.db_id:
                db.update_position(pos.db_id, stop_price=pos.stop_price,
                                   updated_at=bar.ts.isoformat())

    def _close(self, pos: Position, ref_price: float, ts: datetime, reason: str) -> None:
        trade = self.broker.close_position(pos, ref_price, ts, reason)
        self.n_trades += 1
        self.closed_trades.append(trade)
        self.day.record_trade_result(pos.strategy, trade.net_pnl)
        if self.persist:
            if pos.db_id:
                db.close_position(pos.db_id, ts.isoformat())
            db.record_trade(
                run_id=self.run_id, mode=self.mode, strategy=pos.strategy,
                symbol=pos.symbol, side=pos.side, qty=pos.qty,
                entry_ts=pos.entry_ts.isoformat(), entry_price=pos.entry_price,
                exit_ts=ts.isoformat(), exit_price=trade.exit_price,
                gross_pnl=trade.gross_pnl, costs=trade.costs,
                net_pnl=trade.net_pnl, r_multiple=trade.r_multiple,
                planned_stop=pos.planned_stop, planned_target=pos.target_price,
                exit_reason=reason,
            )
        emoji = "+" if trade.net_pnl >= 0 else "-"
        self._event("exit", f"{pos.strategy} {pos.side} {pos.symbol} closed ({reason}) "
                            f"net ₹{trade.net_pnl:,.0f} [{emoji}]")

    def _close_all(self, reason: str) -> None:
        for pos in list(self.broker.positions):
            ref = self.marks.get(pos.symbol, pos.entry_price)
            ts = self.now or clock.now_ist()
            self._close(pos, ref, ts, reason)
        self.pending.clear()

    def _square_off_all(self, reason: str) -> None:
        self._squared_off = True
        if self.broker.positions:
            self._event("squareoff", f"square-off: closing "
                                     f"{len(self.broker.positions)} positions")
        self._close_all(reason)

    # ------------------------------------------------------------- plumbing

    def _mark_equity(self) -> None:
        if not self.persist or self.now is None:
            return
        eq = self.broker.equity(self.marks)
        db.log_equity(
            self.mode, self.now.isoformat(), eq,
            eq - self.broker.margin_used, self.broker.margin_used,
            len(self.broker.positions), eq - self.day.start_equity,
        )

    def _heartbeat(self) -> None:
        """Status snapshot for the dashboard's 'running strategies' panel."""
        if not self.persist or self.now is None:
            return
        import json
        eq = self.broker.equity(self.marks)
        db.kv_set("engine_heartbeat", json.dumps({
            "ts": self.now.isoformat(timespec="seconds"),
            "wall_ts": clock.now_ist().isoformat(timespec="seconds"),
            "mode": self.mode,
            "phase": clock.phase(self.now),
            "feed": self.feed.source_name,
            "equity": round(eq),
            "day_pnl": round(eq - self.day.start_equity),
            "open_positions": len(self.broker.positions),
            "entries_today": self.day.entries_today,
            "entries_budget": config.MAX_ENTRIES_PER_DAY,
            "halted": self.day.halted,
            "halt_reason": self.day.halt_reason,
            "strategies": [s.name for s in self.strategies],
            "benched": sorted(self.day.benched_strategies),
            "trades_today": self.n_trades,
        }))

    def _log_skip(self, strategy: str | None, symbol: str | None, reason: str) -> None:
        if self.persist:
            db.log_skip((self.now or clock.now_ist()).isoformat(), self.mode,
                        strategy, symbol, reason)
        self._event("skip", f"SKIP {strategy or ''} {symbol or ''}: {reason}")

    def _warn(self, msg: str) -> None:
        log.warning(msg)
        self.warnings.append(msg)

    def _event(self, kind: str, msg: str) -> None:
        log.info("[%s] %s", kind, msg)
        try:
            self.on_event(kind, msg)
        except Exception:  # noqa: BLE001 — dashboard errors never kill trading
            pass

    def _finalize(self) -> None:
        if self.broker.positions:
            self._square_off_all("SQUAREOFF")
        self.feed.stop()
        if self.persist and self.run_id:
            db.finish_run(
                self.run_id, datetime.now().isoformat(timespec="seconds"),
                self.bars_processed, self.n_signals, self.n_trades,
                "; ".join(self.warnings[-20:]),
            )
        self._event("run", f"session done: {self.n_trades} trades, "
                           f"equity ₹{self.broker.equity(self.marks):,.0f}")
