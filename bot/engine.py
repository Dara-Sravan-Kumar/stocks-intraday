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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import config
from bot import alerts, clock, db
from bot.bars import Bar
from bot.execution import Broker, ClosedTrade, Position
from bot.indicators import atr_stop_floor
from bot.feeds import Feed
from bot.risk import Approval, DayState, RiskEngine, Skip
from bot.state import MarketState
from bot.strategies import Signal, Strategy

log = logging.getLogger(__name__)

INDEX_NAMES = set(config.INDEX_SYMBOLS)

# How often to re-nudge the operator on Discord while the paper book stays
# frozen (no real Fyers feed). Mirrors the MCX sibling's 60-min throttle.
FREEZE_REMINDER_MINUTES = 60


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
                 require_fyers_feed: bool = False,
                 freeze_reminder_minutes: int = FREEZE_REMINDER_MINUTES,
                 on_event=None):
        self.mode = mode
        self.feed = feed
        self.broker = broker
        self.strategies = strategies
        self.risk = risk
        self.market = market
        self.persist = persist
        self.idle_sleep = idle_sleep
        # Production paper policy (matches the MCX bot): the paper book may only
        # be OPENED / CLOSED / MARKED on the real Fyers feed. When True and the
        # feed is a yfinance fallback/degrade (or any non-Fyers source), the book
        # is FROZEN — we keep scanning/logging/alerting but touch nothing. Left
        # False for backtests/replays and the intentional --feed yf dev run.
        self.require_fyers_feed = require_fyers_feed
        # Re-nudge the operator (Discord) roughly this often while frozen. The
        # timer runs on engine time (bar-driven `self.now`, wall clock as a
        # fallback) so it's deterministic in tests. None => never reminded yet.
        self.freeze_reminder_minutes = freeze_reminder_minutes
        self._last_freeze_alert: datetime | None = None
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

        # Book policy: on a non-Fyers/degraded feed the book is frozen — we still
        # advance indicators (scanning) but never open, close, or mark positions.
        frozen = self._book_frozen()
        if frozen:
            self._announce_freeze()
        else:
            self._reset_freeze_reminder()

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

            if not frozen:
                self._fill_pending(bar, phase)
                self._check_price_exits(bar)
            if st.on_bar_1m(bar) is not None:
                completed_5m.append(bar.symbol)
            if not frozen:
                self._manage_positions(bar)

        if frozen:
            pass  # book frozen: no square-off, no daily-loss close on stale marks
        elif phase == clock.SQUAREOFF and not self._squared_off:
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

    def _feed_degraded(self) -> bool:
        """True when a real-time feed fell back mid-session (source contains
        'degraded'). Its data is laggy, so NEW entries are paused while degraded
        — open positions are still managed and closed. An intentional --feed yf
        run (source 'yfinance', without 'degraded') is NOT paused."""
        return "degraded" in self.feed.source_name.lower()

    def _book_frozen(self) -> bool:
        """The paper book is FROZEN unless the data source is the real Fyers
        feed. Under require_fyers_feed (production paper runs), a yfinance
        fallback/degrade — or any non-Fyers source — means prices aren't
        trustworthy, so NO position is opened, closed, or marked: we scan, log,
        and alert only. Backtests/replays and the intentional --feed yf dev run
        set require_fyers_feed=False and are never frozen."""
        if not self.require_fyers_feed:
            return False
        src = self.feed.source_name.lower()
        return (not src.startswith("fyers-ws")) or "degraded" in src or "aborted" in src

    def _announce_freeze(self) -> None:
        """Nudge the operator that the book is FROZEN — immediately on entry, then
        re-send roughly every `freeze_reminder_minutes` while it stays frozen, so
        the daily-Fyers-login reminder keeps arriving until they act. Timed on
        engine time (self.now, wall clock as a fallback) for deterministic tests."""
        now = self.now or clock.now_ist()
        last = self._last_freeze_alert
        if last is not None and (now - last) < timedelta(minutes=self.freeze_reminder_minutes):
            return
        self._last_freeze_alert = now
        first = last is None
        self._event("freeze", f"book FROZEN — no real Fyers feed "
                              f"(feed: {self.feed.source_name}); "
                              + ("scanning/logging only" if first
                                 else "still frozen — hourly login reminder"))
        msg = ("🧊 Fyers login missing / feed degraded — paper book is FROZEN, "
               f"no trades booked (feed: {self.feed.source_name}). Run the daily "
               "Fyers login (one login covers all 3 bots; deadline before 08:45).")
        try:
            alerts.send(msg)
        except Exception:  # noqa: BLE001 — an alert failure must never kill the loop
            pass

    def _reset_freeze_reminder(self) -> None:
        """Feed recovered — clear the timer so a later degrade re-alerts promptly."""
        self._last_freeze_alert = None

    def _collect_signals(self, symbols: list[str]) -> None:
        if not clock.entries_allowed(self.now):
            return
        if self._book_frozen():
            self._announce_freeze()
            self._log_skip(None, None,
                           "book frozen — no real Fyers feed; new entries paused")
            return
        if self._feed_degraded():
            self._log_skip(None, None,
                           "feed degraded to yfinance fallback — new entries paused")
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
                    # per-VARIANT pending check: a variant skips only instruments
                    # IT already has queued; other variants may still take them.
                    if any(p.signal.symbol == sig.symbol and p.signal.variant == sig.variant
                           for p in self.pending):
                        self._log_skip(sig.variant, sig.symbol, "entry already pending")
                        continue
                    if sig.symbol == sym and not self.risk.regime_allows(sig.side, self.market):
                        # regime filter applies to directional equity/index trades,
                        # not multi-leg option structures on another instrument
                        self._log_skip(sig.strategy, sym,
                                       f"regime filter: NIFTY against {sig.side}")
                        continue
                    sig = self._apply_atr_stop_floor(sig, strat, st, sym)
                    candidates.append((st.ind.rvol() or 0.0, sig))

        # Phase 2: strongest activity first — the day's entry budget goes to
        # the highest-conviction signals, not the alphabetically first ones.
        candidates.sort(key=lambda c: c[0], reverse=True)
        for _, sig in candidates:
            if any(p.signal.symbol == sig.symbol and p.signal.variant == sig.variant
                   for p in self.pending):
                self._log_skip(sig.variant, sig.symbol,
                               "entry already pending for this variant")
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
                variant_key=sig.variant,
            )
            if isinstance(res, Skip):
                self._log_skip(sig.variant, sig.symbol, res.reason)
                continue
            self.day.entries_today += 1
            self.pending.append(PendingEntry(sig, res, self.now))
            self._event("signal", f"{sig.strategy} {sig.side} {sig.symbol}: {sig.reason} "
                                  f"(qty {res.qty})")

    def _fill_pending(self, bar: Bar, phase: str) -> None:
        if self._book_frozen():
            for p in [p for p in self.pending if p.signal.symbol == bar.symbol]:
                self.pending.remove(p)
                self._log_skip(p.signal.strategy, bar.symbol,
                               "book frozen — no real Fyers feed; entry not opened")
            return
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
                margin=ap.margin, instrument=instrument, variant_key=sig.variant,
            )
            if pos is None:
                self._log_skip(sig.variant, sig.symbol, "broker rejected entry")
                continue
            pos.mode = self.mode
            strat = self._strategy_by_name.get(sig.strategy)
            if strat:
                strat.note_entry(sig.symbol, sig.side)
            if self.persist:
                pos.db_id = db.open_position(
                    run_id=self.run_id, mode=self.mode, strategy=sig.strategy,
                    variant_key=sig.variant,
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

    def _bars_held(self, pos: Position) -> int:
        """STRATEGY-timeframe (5m) bars elapsed since a position was opened."""
        now = self.now or pos.entry_ts
        mins = (now - pos.entry_ts).total_seconds() / 60.0
        return int(max(0.0, mins) // max(1, config.STRATEGY_INTERVAL_MIN))

    def _apply_atr_stop_floor(self, sig: Signal, strat: Strategy,
                              st, sym: str) -> Signal:
        """Widen a noise-tight structure stop to MIN_STOP_ATR_MULT x ATR (clamped
        to the strategy's max-risk ceiling). Equity classics only — options
        (premium stops) and the discovered channels (flat gated stops) opt out."""
        if (not getattr(strat, "use_atr_stop_floor", False)
                or strat.requires_options
                or sig.symbol != sym or st is None or st.option_meta is not None
                or not st.bars_5m):
            return sig
        atr = st.ind.atr14.value
        if not atr:
            return sig   # ATR not ready — leave the stop as the strategy set it
        ref = st.bars_5m[-1].close
        ceiling = strat.p.get("max_risk_pct", config.ATR_STOP_FLOOR_MAX_RISK_PCT)
        new_stop = atr_stop_floor(ref, sig.stop, atr, sig.side,
                                  min_stop_atr_mult=config.MIN_STOP_ATR_MULT,
                                  max_risk_pct=ceiling)
        if new_stop is not None and new_stop != sig.stop:
            return replace(sig, stop=new_stop)
        return sig

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
                # Grace period: a SOFT "setup broken" exit reads the instrument's
                # absolute state, which is often already broken at entry — suppress
                # it until the position has been held the minimum number of bars.
                # Hard stop/target (engine) and square-off are never gated here.
                if req.soft and self._bars_held(pos) < config.MIN_HOLD_BARS_BEFORE_SOFT_EXIT:
                    if self.persist and pos.db_id:
                        db.update_position(pos.db_id, stop_price=pos.stop_price,
                                           updated_at=bar.ts.isoformat())
                    continue
                self._close(pos, bar.close, bar.ts, req.reason)
            elif self.persist and pos.db_id:
                db.update_position(pos.db_id, stop_price=pos.stop_price,
                                   updated_at=bar.ts.isoformat())

    def _close(self, pos: Position, ref_price: float, ts: datetime, reason: str) -> None:
        if self._book_frozen():
            # No trustworthy price: leave the position OPEN rather than close it
            # on a laggy/wrong fallback mark. A crashed/left-open position is
            # reconciled by abandon_stale_positions on the next real run.
            self._announce_freeze()
            return
        trade = self.broker.close_position(pos, ref_price, ts, reason)
        self.n_trades += 1
        self.closed_trades.append(trade)
        self.day.record_trade_result(pos.variant, trade.net_pnl)
        if self.persist:
            if pos.db_id:
                db.close_position(pos.db_id, ts.isoformat())
            db.record_trade(
                run_id=self.run_id, mode=self.mode, strategy=pos.strategy,
                variant_key=pos.variant,
                symbol=pos.symbol, side=pos.side, qty=pos.qty,
                entry_ts=pos.entry_ts.isoformat(), entry_price=pos.entry_price,
                exit_ts=ts.isoformat(), exit_price=trade.exit_price,
                gross_pnl=trade.gross_pnl, costs=trade.costs,
                net_pnl=trade.net_pnl, r_multiple=trade.r_multiple,
                planned_stop=pos.planned_stop, planned_target=pos.target_price,
                exit_reason=reason, feed_source=self.feed.source_name,
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
        if self._book_frozen():
            return  # don't mark the book on an untrustworthy (non-Fyers) feed
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
        if self.broker.positions and not self._book_frozen():
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
