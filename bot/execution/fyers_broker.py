"""Live order routing to Fyers — HARD-GATED, same paper-book-plus-live-mirror
design as the Dhan path (see dhan_broker.py for the full design note).

Gates (each one independently blocks live orders):
  1. config.LIVE_TRADING_ENABLED is True            (edit config.py)
  2. .env LIVE_CONFIRM == config.LIVE_CONFIRM_STRING
  3. Fyers credentials present + a valid access token
  4. run_live.py launched with --live               (checked by run_live)
  5. per-order: strategy in config.LIVE_STRATEGY_ALLOWLIST
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime

import config
from bot import clock, db, fyers_auth
from bot.execution import LONG, Broker, ClosedTrade, Position
from bot.execution.dhan_broker import LiveTradingBlocked
from bot.execution.paper_broker import PaperBroker

log = logging.getLogger(__name__)


def check_live_gates() -> None:
    if not config.LIVE_TRADING_ENABLED:
        raise LiveTradingBlocked("gate 1: config.LIVE_TRADING_ENABLED is False")
    if config.live_confirm() != config.LIVE_CONFIRM_STRING:
        raise LiveTradingBlocked(
            "gate 2: .env LIVE_CONFIRM does not match the confirmation string")
    if not fyers_auth.has_credentials():
        raise LiveTradingBlocked("gate 3: FYERS_APP_ID / FYERS_SECRET_ID missing")
    if not config.LIVE_STRATEGY_ALLOWLIST:
        raise LiveTradingBlocked("gate 5: LIVE_STRATEGY_ALLOWLIST is empty")


def _make_client():
    """Isolated for tests to monkeypatch."""
    from fyers_apiv3 import fyersModel
    token = fyers_auth.ensure_access_token()
    if token is None:
        raise LiveTradingBlocked("gate 3: no valid Fyers access token "
                                 "(run: python -m bot.fyers_auth)")
    return fyersModel.FyersModel(client_id=config.fyers_settings()["app_id"],
                                 token=token, is_async=False, log_path="")


class FyersHybridBroker(Broker):
    """Paper book + gated Fyers live mirror for allowlisted strategies."""

    def __init__(self, paper: PaperBroker, client=None):
        check_live_gates()
        self.paper = paper
        self.client = client if client is not None else _make_client()
        self.live_positions = 0
        log.warning("LIVE (Fyers) order mirroring ACTIVE for strategies: %s",
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
        from bot.feeds.fyers_feed import fyers_symbol
        side = "BUY" if buy else "SELL"
        try:
            resp = self.client.place_order({
                "symbol": fyers_symbol(symbol),
                "qty": qty,
                "type": 2,                     # market
                "side": 1 if buy else -1,
                "productType": "INTRADAY",
                "limitPrice": 0, "stopPrice": 0,
                "validity": "DAY", "disclosedQty": 0,
                "offlineOrder": False, "stopLoss": 0, "takeProfit": 0,
            })
            status = (resp or {}).get("s", "unknown")
            db.record_order(
                mode="LIVE", broker="fyers",
                broker_order_id=str((resp or {}).get("id", "")),
                strategy=strategy, symbol=symbol, side=side, qty=qty,
                order_type="MARKET", price=0.0, status=str(status),
                raw_response=json.dumps(resp, default=str)[:4000],
                ts=ts.isoformat(),
            )
            ok = str(status).lower() == "ok"
            if not ok:
                log.error("live %s %s x%d rejected: %s", side, symbol, qty, resp)
            return ok
        except Exception as exc:  # noqa: BLE001 — live failure must not stop paper flow
            log.error("live %s %s x%d failed: %s", side, symbol, qty, exc)
            db.record_order(
                mode="LIVE", broker="fyers", broker_order_id="",
                strategy=strategy, symbol=symbol, side=side, qty=qty,
                order_type="MARKET", price=0.0, status="error",
                raw_response=str(exc)[:1000], ts=ts.isoformat(),
            )
            return False

    # -- Broker interface ---------------------------------------------------------

    def open_position(self, strategy: str, symbol: str, side: str, qty: int,
                      ref_price: float, ts: datetime, stop: float,
                      target: float | None, margin: float | None = None,
                      instrument: str = "EQ") -> Position | None:
        pos = self.paper.open_position(strategy, symbol, side, qty,
                                       ref_price, ts, stop, target,
                                       margin=margin, instrument=instrument)
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
                log.warning("LIVE entry mirrored (fyers): %s %s %s x%d",
                            strategy, side, symbol, live_qty)
        return pos

    def close_position(self, pos: Position, ref_price: float, ts: datetime,
                       reason: str) -> ClosedTrade:
        live_qty = pos.scratch.get("live_qty")
        if live_qty:
            self._place_market(pos.symbol, buy=(not pos.is_long), qty=live_qty,
                               strategy=pos.strategy, ts=ts)
            self.live_positions = max(0, self.live_positions - 1)
            log.warning("LIVE exit mirrored (fyers): %s %s x%d (%s)",
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
