import sqlite3
import os
import threading

DB_PATH = os.environ.get("DB_PATH", "trades.db")

_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                symbol TEXT,
                side TEXT,
                qty REAL,
                entry_price REAL,
                exit_price REAL,
                closed_pnl REAL,
                created_time INTEGER,
                updated_time INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.commit()
        conn.close()


def get_last_sync_time() -> int:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT value FROM meta WHERE key = 'last_sync_ms'").fetchone()
        conn.close()
    return int(row["value"]) if row else 0


def set_last_sync_time(ts_ms: int):
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('last_sync_ms', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(ts_ms),),
        )
        conn.commit()
        conn.close()


def upsert_trades(trades: list[dict]):
    """trades: список словарей из Bybit get_closed_pnl (raw)."""
    if not trades:
        return 0
    with _lock:
        conn = get_conn()
        count = 0
        for t in trades:
            trade_id = t.get("orderId") or t.get("execId") or f"{t.get('symbol')}_{t.get('updatedTime')}"
            try:
                conn.execute(
                    """
                    INSERT INTO trades (id, symbol, side, qty, entry_price, exit_price, closed_pnl, created_time, updated_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (
                        trade_id,
                        t.get("symbol"),
                        t.get("side"),
                        float(t.get("qty", 0) or 0),
                        float(t.get("avgEntryPrice", 0) or 0),
                        float(t.get("avgExitPrice", 0) or 0),
                        float(t.get("closedPnl", 0) or 0),
                        int(t.get("createdTime", 0) or 0),
                        int(t.get("updatedTime", 0) or 0),
                    ),
                )
                count += 1
            except Exception:
                continue
        conn.commit()
        conn.close()
    return count


def query_stats(start_ms: int, end_ms: int):
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM trades WHERE updated_time BETWEEN ? AND ? ORDER BY updated_time ASC",
            (start_ms, end_ms),
        ).fetchall()
        conn.close()
    return [dict(r) for r in rows]


def total_trade_count() -> int:
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()
        conn.close()
    return row["c"] if row else 0
