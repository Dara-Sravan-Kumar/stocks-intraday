"""Live order routing to Dhan — HARD-GATED, never enabled by default.

Design: the paper book stays the single source of truth for every strategy.
When ALL live gates pass, HybridBroker additionally MIRRORS the entries/exits
of allowlisted strategies as real MIS market orders on Dhan, at reduced size
(LIVE_CAPITAL / LIVE_RISK_PER_TRADE_PCT scaling). Every real order and raw
response is written to the orders table.

Gates (each one independently blocks live orders):
  1. config.LIVE_TRADING_ENABLED is True            (edit config.py)
  2. .env DHAN_LIVE_CONFIRM == config.LIVE_CONFIRM_STRING
  3. Dhan credentials present in .env
  4. run_live.py launched with --live               (checked by run_live)
  5. per-order: strategy in config.LIVE_STRATEGY_ALLOWLIST
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime

import config
from bot import clock, db
from bot.execution import LONG, Broker, ClosedTrade, Position
from bot.execution.paper_broker import PaperBroker

log = logging.getLogger(__name__)


class LiveTradingBlocked(Exception):
    """Raised when any live gate fails."""


def check_live_gates() -> None:
    """Global gates 1-3. Raises LiveTradingBlocked naming the failing gate."""
    if not config.LIVE_TRADING_ENABLED:
        raise LiveTradingBlocked("gate 1: config.LIVE_TRADING_ENABLED is False")
    s = config.dhan_settings()
    if config.live_confirm() != config.LIVE_CONFIRM_STRING:
        raise LiveTradingBlocked(
            "gate 2: .env LIVE_CONFIRM/DHAN_LIVE_CONFIRM does not match the confirmation string")
    if not (s["client_id"] and s["access_token"]):
        raise LiveTradingBlocked("gate 3: DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN missing")
    if not config.LIVE_STRATEGY_ALLOWLIST:
        raise LiveTradingBlocked("gate 5: LIVE_STRATEGY_ALLOWLIST is empty")


def _make_client():
    """Isolated for tests to monkeypatch."""
    from dhanhq import DhanContext, dhanhq
    s = config.dhan_settings()
    return dhanhq(DhanContext(s["client_id"], s["access_token"]))


class HybridBroker(Broker):
    """Paper book + gated live mirror for allowlisted strategies."""

    def __init__(self, paper: PaperBroker, client=None,
                 security_ids: dict[str, str] | None = None):
        check_live_gates()
        self.paper = paper
        self.client = client if client is not None else _make_client()
        if security_ids is None:
            security_ids = {
                r["symbol"]: r["dhan_security_id"]
                for r in db.load_universe() if r["dhan_security_id"]
            }
        self.security_ids = security_ids
        self.live_positions = 0
        log.warning("LIVE order mirroring ACTIVE for strategies: %s",
                    sorted(config.LIVE_STRATEGY_ALLOWLIST))

    # -- book delegation (paper stays the book of record) ---------------------

    @property
    def positions(self) -> list[Position]:
        return self.paper.positions

    @property
    def margin_used(self) -> float:
        return self.paper.margin_used

    def equity(self, marks: dict[str, float]) -> float:
        return self.paper.equity(marks)

    # -- live sizing -----------------------------------------------------------

    def _live_qty(self, paper_qty: int, paper_equity: float) -> int:
        capital_scale = config.LIVE_CAPITAL / max(paper_equity, 1.0)
        risk_scale = config.LIVE_RISK_PER_TRADE_PCT / config.RISK_PER_TRADE_PCT
        return max(0, math.floor(paper_qty * capital_scale * risk_scale))

    # -- order plumbing ---------------------------------------------------------

    def _place_market(self, symbol: str, buy: bool, qty: int,
                      strategy: str, ts: datetime) -> bool:
        sec_id = self.security_ids.get(symbol)
        if not sec_id:
            log.error("live order skipped: no security id for %s", symbol)
            return False
        side = "BUY" if buy else "SELL"
        try:
            resp = self.client.place_order(
                security_id=str(sec_id),
                exchange_segment=self.client.NSE,
                transaction_type=self.client.BUY if buy else self.client.SELL,
                quantity=qty,
                order_type=self.client.MARKET,
                product_type=self.client.INTRA,
                price=0,
            )
            status = (resp or {}).get("status", "unknown")
            db.record_order(
                mode="LIVE", broker="dhan",
                broker_order_id=str(((resp or {}).get("data") or {}).get("orderId", "")),
                strategy=strategy, symbol=symbol, side=side, qty=qty,
                order_type="MARKET", price=0.0, status=str(status),
                raw_response=json.dumps(resp, default=str)[:4000],
                ts=ts.isoformat(),
            )
            ok = str(status).lower() in ("success", "transit", "pending", "traded")
            if not ok:
                log.error("live %s %s x%d rejected: %s", side, symbol, qty, resp)
            return ok
        except Exception as exc:  # noqa: BLE001 — live failure must not stop paper flow
            log.error("live %s %s x%d failed: %s", side, symbol, qty, exc)
            db.record_order(
                mode="LIVE", broker="dhan", broker_order_id="",
                strategy=strategy, symbol=symbol, side=side, qty=qty,
                order_type="MARKET", price=0.0, status="error",
                raw_response=str(exc)[:1000], ts=ts.isoformat(),
            )
            return False

    # -- Broker interface ---------------------------------------------------------

    def open_position(self, strategy: str, symbol: str, side: str, qty: int,
                      ref_price: float, ts: datetime, stop: float,
                      target: float | None, margin: float | None = None,
                      instrument: str = "EQ",
                      variant_key: str = "") -> Position | None:
        pos = self.paper.open_position(strategy, symbol, side, qty,
                                       ref_price, ts, stop, target,
                                       margin=margin, instrument=instrument,
                                       variant_key=variant_key)
        if pos is None:
            return None
        if instrument != "EQ":
            return pos   # live mirroring is equity-only for now
        if strategy in config.LIVE_STRATEGY_ALLOWLIST \
                and self.live_positions < config.LIVE_MAX_CONCURRENT_POSITIONS:
            live_qty = self._live_qty(qty, self.paper.equity({symbol: ref_price}))
            if live_qty >= 1 and self._place_market(
                    symbol, buy=(side == LONG), qty=live_qty,
                    strategy=strategy, ts=ts):
                pos.scratch["live_qty"] = live_qty
                self.live_positions += 1
                log.warning("LIVE entry mirrored: %s %s %s x%d",
                            strategy, side, symbol, live_qty)
        return pos

    def close_position(self, pos: Position, ref_price: float, ts: datetime,
                       reason: str) -> ClosedTrade:
        live_qty = pos.scratch.get("live_qty")
        if live_qty:
            self._place_market(pos.symbol, buy=(not pos.is_long), qty=live_qty,
                               strategy=pos.strategy, ts=ts)
            self.live_positions = max(0, self.live_positions - 1)
            log.warning("LIVE exit mirrored: %s %s x%d (%s)",
                        pos.strategy, pos.symbol, live_qty, reason)
        return self.paper.close_position(pos, ref_price, ts, reason)

    def kill_switch(self) -> None:
        """Emergency: market-close every live-mirrored position immediately."""
        now = clock.now_ist()
        for pos in list(self.paper.positions):
            live_qty = pos.scratch.get("live_qty")
            if live_qty:
                self._place_market(pos.symbol, buy=(not pos.is_long),
                                   qty=live_qty, strategy=pos.strategy, ts=now)
                pos.scratch.pop("live_qty", None)
        self.live_positions = 0
