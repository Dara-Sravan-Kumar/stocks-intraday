"""Index option chains built from the Fyers NSE_FO symbol master.

Every contract fact (symbol, expiry, strike, LOT SIZE) comes from the master
file — nothing is hardcoded, so SEBI lot-size changes and expiry-day moves are
absorbed automatically. Master columns (no header, positional):
  0 fyToken | 1 description | 2 instrument type (14 = index option) | 3 lot
  4 tick | 8 expiry epoch | 9 fyers symbol | 13 underlying | 15 strike | 16 CE/PE
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime

import requests

import config
from bot import clock

log = logging.getLogger(__name__)

INSTRUMENT_INDEX_OPTION = "14"


@dataclass(frozen=True)
class OptionContract:
    symbol: str            # full Fyers symbol, e.g. NSE:NIFTY26JUL24500CE
    underlying: str        # NIFTY | BANKNIFTY
    expiry: date
    strike: float
    opt_type: str          # CE | PE
    lot: int


def _master_path():
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return config.CACHE_DIR / "fyers-nse-fo.csv"


def _load_master_text() -> str | None:
    path = _master_path()
    if path.exists():
        age_h = (datetime.now().timestamp() - path.stat().st_mtime) / 3600
        if age_h <= config.OPTIONS["master_cache_hours"]:
            return path.read_text(encoding="utf-8", errors="replace")
    try:
        resp = requests.get(config.OPTIONS["master_url"], timeout=120)
        resp.raise_for_status()
        path.write_text(resp.text, encoding="utf-8")
        return resp.text
    except Exception as exc:  # noqa: BLE001
        log.warning("NSE_FO master download failed: %s", exc)
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None


def load_contracts(underlyings: list[str] | None = None) -> list[OptionContract]:
    """All live index option contracts for the given underlyings."""
    underlyings = underlyings or config.OPTIONS["underlyings"]
    wanted = set(underlyings)
    text = _load_master_text()
    if text is None:
        return []
    out: list[OptionContract] = []
    for row in csv.reader(text.splitlines()):
        try:
            if len(row) < 17 or row[2].strip() != INSTRUMENT_INDEX_OPTION:
                continue
            underlying = row[13].strip()
            if underlying not in wanted:
                continue
            expiry = datetime.fromtimestamp(int(row[8]), tz=clock.IST).date()
            out.append(OptionContract(
                symbol=row[9].strip(), underlying=underlying, expiry=expiry,
                strike=float(row[15]), opt_type=row[16].strip(),
                lot=int(row[3]),
            ))
        except (ValueError, IndexError):
            continue
    return out


def nearest_expiry(contracts: list[OptionContract], on_or_after: date) -> date | None:
    expiries = sorted({c.expiry for c in contracts if c.expiry >= on_or_after})
    return expiries[0] if expiries else None


def build_chain(underlying: str, spot: float, session_date: date,
                contracts: list[OptionContract] | None = None,
                n_strikes: int | None = None) -> list[OptionContract]:
    """Nearest-expiry contracts within ATM ± n strikes, CE and PE."""
    n = n_strikes if n_strikes is not None else config.OPTIONS["n_strikes_each_side"]
    contracts = contracts if contracts is not None else load_contracts([underlying])
    mine = [c for c in contracts if c.underlying == underlying]
    expiry = nearest_expiry(mine, session_date)
    if expiry is None:
        return []
    at_expiry = [c for c in mine if c.expiry == expiry]
    strikes = sorted({c.strike for c in at_expiry})
    if not strikes:
        return []
    atm = min(strikes, key=lambda s: abs(s - spot))
    idx = strikes.index(atm)
    window = set(strikes[max(0, idx - n): idx + n + 1])
    chain = [c for c in at_expiry if c.strike in window]
    chain.sort(key=lambda c: (c.strike, c.opt_type))
    return chain


def pick_option(chain: list[OptionContract], spot: float, opt_type: str,
                steps_itm: int = 0) -> OptionContract | None:
    """ATM contract of the given type; steps_itm>0 moves in-the-money."""
    side = [c for c in chain if c.opt_type == opt_type]
    if not side:
        return None
    strikes = sorted({c.strike for c in side})
    atm = min(strikes, key=lambda s: abs(s - spot))
    idx = strikes.index(atm)
    if opt_type == "CE":
        idx = max(0, idx - steps_itm)
    else:
        idx = min(len(strikes) - 1, idx + steps_itm)
    target = strikes[idx]
    for c in side:
        if c.strike == target:
            return c
    return None


def spot_price(underlying: str) -> float | None:
    """Latest index level: Fyers quote if a token exists, else cached bars."""
    from bot import db, fyers_auth
    try:
        token = fyers_auth.ensure_access_token()
        if token:
            from fyers_apiv3 import fyersModel
            fy = fyersModel.FyersModel(
                client_id=config.fyers_settings()["app_id"], token=token,
                is_async=False, log_path="")
            fy_sym = config.FYERS_INDEX_SYMBOLS[underlying]
            resp = fy.quotes({"symbols": fy_sym})
            for d in (resp or {}).get("d") or []:
                lp = (d.get("v") or {}).get("lp")
                if lp:
                    return float(lp)
    except Exception as exc:  # noqa: BLE001
        log.warning("spot quote failed for %s: %s", underlying, exc)
    row = db.connect().execute(
        "SELECT close FROM bars_1m WHERE symbol=? ORDER BY ts DESC LIMIT 1",
        (underlying,),
    ).fetchone()
    return float(row["close"]) if row else None
