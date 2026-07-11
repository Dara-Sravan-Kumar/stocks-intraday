"""Universe management: Nifty50 + BankNifty constituents and Dhan security-id mapping.

Fallback chain for the universe: fresh CSV fetch -> DB cache -> static config lists.
The Dhan scrip master is a public CSV (no auth) cached on disk.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests

import config
from bot import db

log = logging.getLogger(__name__)


@dataclass
class Instrument:
    symbol: str                      # NSE trading symbol, e.g. "RELIANCE"
    name: str = ""
    indices: set[str] = field(default_factory=set)
    dhan_security_id: str | None = None

    @property
    def yf_symbol(self) -> str:
        # Yahoo uses '-' where NSE uses '&' etc.; M&M -> M&M.NS works as-is.
        return f"{self.symbol}{config.YF_SUFFIX}"


def _fetch_index_csv(index: str) -> list[dict] | None:
    for url in (config.UNIVERSE_CSV_URLS[index], config.UNIVERSE_CSV_MIRRORS[index]):
        try:
            resp = requests.get(url, headers=config.UNIVERSE_HTTP_HEADERS, timeout=15)
            resp.raise_for_status()
            rows = list(csv.DictReader(io.StringIO(resp.text)))
            if rows and "Symbol" in rows[0]:
                return rows
        except Exception as exc:  # noqa: BLE001 — degrade, never crash
            log.warning("universe fetch failed for %s via %s: %s", index, url, exc)
    return None


def refresh_universe(force: bool = False) -> dict[str, Instrument]:
    """Fetch constituent lists if stale, persist to DB, return instruments."""
    last = db.kv_get("last_universe_refresh")
    stale = True
    if last and not force:
        stale = datetime.fromisoformat(last) < datetime.now() - timedelta(
            days=config.UNIVERSE_REFRESH_DAYS
        )

    instruments: dict[str, Instrument] = {}
    fetched_any = False
    if stale or force:
        for index in ("NIFTY50", "BANKNIFTY"):
            rows = _fetch_index_csv(index)
            if rows is None:
                continue
            fetched_any = True
            for row in rows:
                sym = row["Symbol"].strip()
                inst = instruments.setdefault(sym, Instrument(symbol=sym))
                inst.name = row.get("Company Name", "").strip() or inst.name
                inst.indices.add(index)
        if fetched_any:
            now = datetime.now().isoformat(timespec="seconds")
            db.upsert_universe([
                (i.symbol, i.name, ",".join(sorted(i.indices)), i.dhan_security_id, now)
                for i in instruments.values()
            ])
            db.kv_set("last_universe_refresh", now)
            log.info("universe refreshed: %d symbols", len(instruments))

    if not instruments:  # cache path
        for row in db.load_universe():
            inst = Instrument(
                symbol=row["symbol"], name=row["name"] or "",
                indices=set((row["index_membership"] or "").split(",")) - {""},
                dhan_security_id=row["dhan_security_id"],
            )
            instruments[inst.symbol] = inst

    if not instruments:  # static fallback
        log.warning("universe: using static fallback lists from config")
        for sym in config.FALLBACK_NIFTY50:
            instruments.setdefault(sym, Instrument(symbol=sym)).indices.add("NIFTY50")
        for sym in config.FALLBACK_BANKNIFTY:
            instruments.setdefault(sym, Instrument(symbol=sym)).indices.add("BANKNIFTY")

    return instruments


def _scrip_master_path():
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return config.CACHE_DIR / "dhan-scrip-master.csv"


def _download_scrip_master() -> str | None:
    try:
        resp = requests.get(config.DHAN_SCRIP_MASTER_URL, timeout=60)
        resp.raise_for_status()
        path = _scrip_master_path()
        path.write_text(resp.text, encoding="utf-8")
        return resp.text
    except Exception as exc:  # noqa: BLE001
        log.warning("dhan scrip master download failed: %s", exc)
        return None


def map_dhan_security_ids(instruments: dict[str, Instrument]) -> int:
    """Fill dhan_security_id on each instrument from the public scrip master CSV.

    Returns the number of symbols mapped. Safe to call without a Dhan account.
    """
    path = _scrip_master_path()
    text: str | None = None
    if path.exists():
        age_days = (datetime.now().timestamp() - path.stat().st_mtime) / 86400
        if age_days <= config.DHAN_SCRIP_CACHE_DAYS:
            text = path.read_text(encoding="utf-8", errors="replace")
    if text is None:
        text = _download_scrip_master()
    if text is None and path.exists():  # stale cache beats nothing
        text = path.read_text(encoding="utf-8", errors="replace")
    if text is None:
        return 0

    wanted = {i.symbol for i in instruments.values()}
    mapped = 0
    reader = csv.DictReader(io.StringIO(text))
    cols = {c.upper(): c for c in (reader.fieldnames or [])}

    def col(*names: str) -> str | None:
        for n in names:
            if n in cols:
                return cols[n]
        return None

    c_exch = col("SEM_EXM_EXCH_ID", "EXCH_ID")
    c_seg = col("SEM_SEGMENT", "SEGMENT")
    c_sym = col("SEM_TRADING_SYMBOL", "SYMBOL_NAME", "TRADING_SYMBOL")
    c_id = col("SEM_SMST_SECURITY_ID", "SECURITY_ID")
    c_type = col("SEM_EXCH_INSTRUMENT_TYPE", "INSTRUMENT_TYPE", "INSTRUMENT")
    if not all((c_exch, c_sym, c_id)):
        log.warning("dhan scrip master: unrecognized columns %s", reader.fieldnames)
        return 0

    for row in reader:
        if row.get(c_exch, "").strip().upper() != "NSE":
            continue
        if c_seg and row.get(c_seg, "").strip().upper() not in ("E", "EQUITY", "NSE_EQ"):
            continue
        if c_type and row.get(c_type, "").strip().upper() not in ("ES", "EQ", "EQUITY", ""):
            continue
        sym = row.get(c_sym, "").strip().upper()
        if sym in wanted and not instruments[sym].dhan_security_id:
            instruments[sym].dhan_security_id = row.get(c_id, "").strip()
            mapped += 1

    if mapped:
        now = datetime.now().isoformat(timespec="seconds")
        db.upsert_universe([
            (i.symbol, i.name, ",".join(sorted(i.indices)), i.dhan_security_id, now)
            for i in instruments.values()
        ])
    log.info("dhan scrip master: mapped %d/%d symbols", mapped, len(wanted))
    return mapped


def load_instruments(refresh: bool = False) -> dict[str, Instrument]:
    """Main entry: universe with Dhan ids attached (best effort)."""
    instruments = refresh_universe(force=refresh)
    missing_ids = [i for i in instruments.values() if not i.dhan_security_id]
    if missing_ids:
        map_dhan_security_ids(instruments)
    return instruments
