"""SQLite persistence. This is the ONLY module that touches the database.

Additive migrations: each entry in MIGRATIONS runs once, tracked in
schema_migrations. Never edit an applied migration — append a new one.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import config

MIGRATIONS: list[str] = [
    # 001 — initial schema
    """
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mode TEXT NOT NULL,
        session_date TEXT NOT NULL,
        feed_source TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        bars_processed INTEGER DEFAULT 0,
        signals INTEGER DEFAULT 0,
        trades INTEGER DEFAULT 0,
        warnings TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS bars_1m (
        symbol TEXT NOT NULL,
        ts TEXT NOT NULL,
        open REAL NOT NULL, high REAL NOT NULL,
        low REAL NOT NULL, close REAL NOT NULL,
        volume INTEGER NOT NULL DEFAULT 0,
        source TEXT DEFAULT '',
        PRIMARY KEY (symbol, ts)
    );
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        mode TEXT NOT NULL,
        strategy TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        qty INTEGER NOT NULL,
        entry_ts TEXT NOT NULL,
        entry_price REAL NOT NULL,
        stop_price REAL NOT NULL,
        target_price REAL,
        margin_used REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'OPEN',
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        mode TEXT NOT NULL,
        strategy TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        qty INTEGER NOT NULL,
        entry_ts TEXT NOT NULL,
        entry_price REAL NOT NULL,
        exit_ts TEXT NOT NULL,
        exit_price REAL NOT NULL,
        gross_pnl REAL NOT NULL,
        costs REAL NOT NULL,
        net_pnl REAL NOT NULL,
        r_multiple REAL,
        planned_stop REAL,
        planned_target REAL,
        exit_reason TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mode TEXT NOT NULL,
        broker TEXT NOT NULL,
        broker_order_id TEXT,
        strategy TEXT, symbol TEXT, side TEXT,
        qty INTEGER, order_type TEXT, price REAL,
        status TEXT, raw_response TEXT, ts TEXT
    );
    CREATE TABLE IF NOT EXISTS equity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mode TEXT NOT NULL,
        ts TEXT NOT NULL,
        equity REAL NOT NULL,
        cash REAL NOT NULL,
        margin_used REAL NOT NULL DEFAULT 0,
        open_positions INTEGER NOT NULL DEFAULT 0,
        day_pnl REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS strategy_stats (
        strategy TEXT NOT NULL,
        session_date TEXT NOT NULL,
        mode TEXT NOT NULL,
        trades INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0,
        gross REAL DEFAULT 0,
        costs REAL DEFAULT 0,
        net REAL DEFAULT 0,
        avg_r REAL,
        max_dd_day REAL,
        PRIMARY KEY (strategy, session_date, mode)
    );
    CREATE TABLE IF NOT EXISTS skips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        mode TEXT NOT NULL,
        strategy TEXT, symbol TEXT,
        reason TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS universe (
        symbol TEXT PRIMARY KEY,
        name TEXT,
        index_membership TEXT,
        dhan_security_id TEXT,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS kv (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_trades_mode_date ON trades (mode, exit_ts);
    CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status, mode);
    CREATE INDEX IF NOT EXISTS idx_bars_ts ON bars_1m (ts);
    """,
    # 002 — multi-variant track records: each strategy VARIANT (a param-set or a
    # discovered spec) gets its own attribution key so many variants can trade the
    # same instrument concurrently and each builds an independent ledger. The
    # one-per-instrument lock is enforced in-memory (bot/risk.py, scoped to the
    # live book) — NOT a DB UNIQUE constraint, which would wrongly block re-entry
    # across sessions. These composite indexes just make per-variant reads fast.
    """
    ALTER TABLE positions ADD COLUMN variant_key TEXT;
    ALTER TABLE trades ADD COLUMN variant_key TEXT;
    UPDATE positions SET variant_key = strategy WHERE variant_key IS NULL;
    UPDATE trades SET variant_key = strategy WHERE variant_key IS NULL;
    CREATE INDEX IF NOT EXISTS idx_trades_variant ON trades (variant_key, exit_ts);
    CREATE INDEX IF NOT EXISTS idx_positions_variant ON positions (variant_key, status);
    """,
    # 003 — discovered strategy fleet: each row is a strategy-as-data spec that
    # trades as a variant of its DISCOVERED channel. name == the variant_key its
    # trades are recorded under, so a spec's ledger is a plain query on trades.
    """
    CREATE TABLE IF NOT EXISTS discovered_specs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        channel TEXT NOT NULL,
        spec_json TEXT NOT NULL,
        entry_expr TEXT NOT NULL,
        canonical_expr TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        gate_json TEXT,
        created_at TEXT NOT NULL,
        retired_at TEXT,
        retire_reason TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_specs_channel_status
        ON discovered_specs (channel, status);
    """,
    # 004 — feed provenance on trades. A trade booked while the real-time feed
    # had degraded to (or was intentionally) the yfinance fallback must be
    # excludable from the promotion track record, which is judged on the
    # production Fyers feed. NULL = legacy/unknown provenance, treated as NOT a
    # fallback so historical trades stay counted.
    """
    ALTER TABLE trades ADD COLUMN feed_source TEXT;
    CREATE INDEX IF NOT EXISTS idx_trades_feed_source ON trades (feed_source);
    """,
]

_local = threading.local()
_db_path_override: Path | None = None


def set_db_path(path: Path | str | None) -> None:
    """Override DB location (tests use ':memory:' via a shared connection)."""
    global _db_path_override
    _db_path_override = Path(path) if path else None
    _local.__dict__.pop("conn", None)


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    path = _db_path_override or config.DB_PATH
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Equity + options sessions run concurrently and share this file; WAL allows
    # one writer at a time, so wait rather than error on a brief write collision.
    conn.execute("PRAGMA busy_timeout=5000")
    _migrate(conn)
    _local.conn = conn
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (id INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    applied = {r["id"] for r in conn.execute("SELECT id FROM schema_migrations")}
    for i, sql in enumerate(MIGRATIONS, start=1):
        if i in applied:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (i,)
        )
    conn.commit()


# --- runs -------------------------------------------------------------------

def start_run(mode: str, session_date: str, feed_source: str, started_at: str) -> int:
    cur = connect().execute(
        "INSERT INTO runs (mode, session_date, feed_source, started_at) VALUES (?,?,?,?)",
        (mode, session_date, feed_source, started_at),
    )
    connect().commit()
    return int(cur.lastrowid)


def finish_run(run_id: int, finished_at: str, bars: int, signals: int,
               trades: int, warnings: str) -> None:
    connect().execute(
        "UPDATE runs SET finished_at=?, bars_processed=?, signals=?, trades=?, warnings=? WHERE id=?",
        (finished_at, bars, signals, trades, warnings, run_id),
    )
    connect().commit()


# --- bars -------------------------------------------------------------------

def upsert_bars(rows: list[tuple]) -> None:
    """rows: (symbol, ts, o, h, l, c, v, source)"""
    connect().executemany(
        "INSERT OR REPLACE INTO bars_1m (symbol, ts, open, high, low, close, volume, source) "
        "VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    connect().commit()


def load_bars(symbols: list[str] | None, start_ts: str, end_ts: str) -> list[sqlite3.Row]:
    q = "SELECT * FROM bars_1m WHERE ts >= ? AND ts <= ?"
    params: list = [start_ts, end_ts]
    if symbols:
        q += f" AND symbol IN ({','.join('?' * len(symbols))})"
        params.extend(symbols)
    q += " ORDER BY ts, symbol"
    return list(connect().execute(q, params))


def bar_dates(symbol: str | None = None) -> list[str]:
    q = "SELECT DISTINCT substr(ts, 1, 10) AS d FROM bars_1m"
    params: tuple = ()
    if symbol:
        q += " WHERE symbol = ?"
        params = (symbol,)
    return [r["d"] for r in connect().execute(q + " ORDER BY d", params)]


# --- positions / trades -----------------------------------------------------

def open_position(**kw) -> int:
    kw.setdefault("variant_key", kw.get("strategy"))
    cur = connect().execute(
        "INSERT INTO positions (run_id, mode, strategy, variant_key, symbol, side, qty, "
        "entry_ts, entry_price, stop_price, target_price, margin_used, status, updated_at) "
        "VALUES (:run_id, :mode, :strategy, :variant_key, :symbol, :side, :qty, :entry_ts, "
        ":entry_price, :stop_price, :target_price, :margin_used, 'OPEN', :entry_ts)",
        kw,
    )
    connect().commit()
    return int(cur.lastrowid)


def update_position(pos_id: int, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    connect().execute(f"UPDATE positions SET {sets} WHERE id=?", (*fields.values(), pos_id))
    connect().commit()


def close_position(pos_id: int, updated_at: str) -> None:
    connect().execute(
        "UPDATE positions SET status='CLOSED', updated_at=? WHERE id=?", (updated_at, pos_id)
    )
    connect().commit()


def open_positions(mode: str) -> list[sqlite3.Row]:
    return list(connect().execute(
        "SELECT * FROM positions WHERE status='OPEN' AND mode=? ORDER BY entry_ts", (mode,)
    ))


def record_trade(**kw) -> int:
    kw.setdefault("variant_key", kw.get("strategy"))
    kw.setdefault("feed_source", None)
    cur = connect().execute(
        "INSERT INTO trades (run_id, mode, strategy, variant_key, symbol, side, qty, entry_ts, "
        "entry_price, exit_ts, exit_price, gross_pnl, costs, net_pnl, r_multiple, planned_stop, "
        "planned_target, exit_reason, feed_source) "
        "VALUES (:run_id, :mode, :strategy, :variant_key, :symbol, :side, :qty, :entry_ts, "
        ":entry_price, :exit_ts, :exit_price, :gross_pnl, :costs, :net_pnl, :r_multiple, "
        ":planned_stop, :planned_target, :exit_reason, :feed_source)",
        kw,
    )
    connect().commit()
    return int(cur.lastrowid)


def trades_for(mode: str, since_date: str | None = None,
               strategy: str | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM trades WHERE mode=?"
    params: list = [mode]
    if since_date:
        q += " AND exit_ts >= ?"
        params.append(since_date)
    if strategy:
        q += " AND strategy=?"
        params.append(strategy)
    return list(connect().execute(q + " ORDER BY exit_ts", params))


def realized_net_pnl(mode: str) -> float:
    row = connect().execute(
        "SELECT COALESCE(SUM(net_pnl), 0) AS s FROM trades WHERE mode=?", (mode,)
    ).fetchone()
    return float(row["s"])


def recent_closed_trades(mode: str, limit: int) -> list[sqlite3.Row]:
    """The most-recent `limit` closed trades for a mode (newest first). Every row
    in `trades` is a closed round trip (it's written on close), so this is simply
    the tail of the ledger — what the daily post-mortem reviews."""
    return list(connect().execute(
        "SELECT * FROM trades WHERE mode=? ORDER BY exit_ts DESC LIMIT ?",
        (mode, int(limit)),
    ))


def variant_ledger_stats(mode: str) -> list[sqlite3.Row]:
    """Per-variant realized performance for a mode: trade count, wins, gross
    win/loss and net — enough to rank variants by profit factor for the
    discovery performance digest."""
    return list(connect().execute(
        "SELECT variant_key, "
        "COUNT(*) AS trades, "
        "COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins, "
        "COALESCE(SUM(CASE WHEN net_pnl > 0 THEN net_pnl ELSE 0 END), 0) AS gross_win, "
        "COALESCE(-SUM(CASE WHEN net_pnl < 0 THEN net_pnl ELSE 0 END), 0) AS gross_loss, "
        "COALESCE(SUM(net_pnl), 0) AS net "
        "FROM trades WHERE mode=? GROUP BY variant_key",
        (mode,),
    ))


# --- orders / equity / skips / stats ---------------------------------------

def record_order(**kw) -> int:
    cur = connect().execute(
        "INSERT INTO orders (mode, broker, broker_order_id, strategy, symbol, side, qty, "
        "order_type, price, status, raw_response, ts) "
        "VALUES (:mode, :broker, :broker_order_id, :strategy, :symbol, :side, :qty, "
        ":order_type, :price, :status, :raw_response, :ts)",
        kw,
    )
    connect().commit()
    return int(cur.lastrowid)


def log_equity(mode: str, ts: str, equity: float, cash: float, margin_used: float,
               open_pos: int, day_pnl: float) -> None:
    connect().execute(
        "INSERT INTO equity_log (mode, ts, equity, cash, margin_used, open_positions, day_pnl) "
        "VALUES (?,?,?,?,?,?,?)",
        (mode, ts, equity, cash, margin_used, open_pos, day_pnl),
    )
    connect().commit()


def log_skip(ts: str, mode: str, strategy: str | None, symbol: str | None, reason: str) -> None:
    connect().execute(
        "INSERT INTO skips (ts, mode, strategy, symbol, reason) VALUES (?,?,?,?,?)",
        (ts, mode, strategy, symbol, reason),
    )
    connect().commit()


def recent_skips(mode: str, limit: int = 20) -> list[sqlite3.Row]:
    return list(connect().execute(
        "SELECT * FROM skips WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit)
    ))


def upsert_strategy_stats(strategy: str, session_date: str, mode: str, **fields) -> None:
    connect().execute(
        "INSERT INTO strategy_stats (strategy, session_date, mode, trades, wins, gross, costs, "
        "net, avg_r, max_dd_day) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(strategy, session_date, mode) DO UPDATE SET trades=excluded.trades, "
        "wins=excluded.wins, gross=excluded.gross, costs=excluded.costs, net=excluded.net, "
        "avg_r=excluded.avg_r, max_dd_day=excluded.max_dd_day",
        (strategy, session_date, mode,
         fields.get("trades", 0), fields.get("wins", 0), fields.get("gross", 0.0),
         fields.get("costs", 0.0), fields.get("net", 0.0), fields.get("avg_r"),
         fields.get("max_dd_day")),
    )
    connect().commit()


# --- universe / kv ----------------------------------------------------------

def upsert_universe(rows: list[tuple]) -> None:
    """rows: (symbol, name, index_membership, dhan_security_id, updated_at)"""
    connect().executemany(
        "INSERT INTO universe (symbol, name, index_membership, dhan_security_id, updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, "
        "index_membership=excluded.index_membership, "
        "dhan_security_id=COALESCE(excluded.dhan_security_id, universe.dhan_security_id), "
        "updated_at=excluded.updated_at",
        rows,
    )
    connect().commit()


def load_universe() -> list[sqlite3.Row]:
    return list(connect().execute("SELECT * FROM universe ORDER BY symbol"))


def kv_get(key: str, default: str | None = None) -> str | None:
    row = connect().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    connect().execute(
        "INSERT INTO kv (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    connect().commit()


def kv_delete(key: str) -> None:
    connect().execute("DELETE FROM kv WHERE key=?", (key,))
    connect().commit()


# --- discovered specs -------------------------------------------------------

def insert_discovered_spec(*, name: str, channel: str, spec_json: str,
                           entry_expr: str, canonical_expr: str, source: str,
                           gate_json: str, created_at: str) -> int:
    cur = connect().execute(
        "INSERT INTO discovered_specs (name, channel, spec_json, entry_expr, "
        "canonical_expr, source, status, gate_json, created_at) "
        "VALUES (?,?,?,?,?,?, 'ACTIVE', ?, ?)",
        (name, channel, spec_json, entry_expr, canonical_expr, source,
         gate_json, created_at),
    )
    connect().commit()
    return int(cur.lastrowid)


def discovered_specs(channel: str | None = None,
                     status: str = "ACTIVE") -> list[sqlite3.Row]:
    q = "SELECT * FROM discovered_specs WHERE 1=1"
    params: list = []
    if status:
        q += " AND status=?"
        params.append(status)
    if channel:
        q += " AND channel=?"
        params.append(channel)
    return list(connect().execute(q + " ORDER BY created_at", params))


def count_discovered_specs(channel: str, status: str = "ACTIVE") -> int:
    row = connect().execute(
        "SELECT COUNT(*) AS n FROM discovered_specs WHERE channel=? AND status=?",
        (channel, status),
    ).fetchone()
    return int(row["n"])


def canonical_exprs(channel: str, status: str = "ACTIVE") -> set[str]:
    return {r["canonical_expr"] for r in connect().execute(
        "SELECT canonical_expr FROM discovered_specs WHERE channel=? AND status=?",
        (channel, status),
    )}


def retire_discovered_spec(name: str, retired_at: str, reason: str) -> None:
    connect().execute(
        "UPDATE discovered_specs SET status='RETIRED', retired_at=?, retire_reason=? "
        "WHERE name=?", (retired_at, reason, name),
    )
    connect().commit()


def variant_stats(variant_key: str, mode: str | None = None) -> dict:
    """Realized ledger for one variant: closed-trade count, wins, net P&L."""
    q = ("SELECT COUNT(*) AS n, "
         "COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins, "
         "COALESCE(SUM(net_pnl), 0) AS net FROM trades WHERE variant_key=?")
    params: list = [variant_key]
    if mode:
        q += " AND mode=?"
        params.append(mode)
    r = connect().execute(q, params).fetchone()
    return {"trades": int(r["n"]), "wins": int(r["wins"]), "net": float(r["net"])}


def bar_symbols(exclude: set[str] | None = None) -> list[str]:
    """Distinct symbols present in bars_1m (for gate sampling)."""
    rows = connect().execute("SELECT DISTINCT symbol FROM bars_1m ORDER BY symbol")
    out = [r["symbol"] for r in rows]
    if exclude:
        out = [s for s in out if s not in exclude]
    return out
