"""
bot/state_store.py

Per-chat toggle state - which of the two 24/7 modes (Phase 2.1
"market_analyse", Phase 2.2 "strong_signal") are ON for a chat, and
which market (Spot/Future/Both) each is running against. Backed by
SQLite at database/bot_state.db, per the file plan (it's git-ignored
runtime data, not source - see .gitignore).

Also the one place that loads database/indicator_toggles.json (the
shipped default indicator on/off map every scan uses) - so handlers
and jobs don't each need their own file-reading logic.

Schema is created on import if it doesn't exist yet - nothing else
needs to run a separate migration step first.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger("crypto-telegram-bot")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT_DIR, "database", "bot_state.db")
INDICATOR_TOGGLES_PATH = os.path.join(ROOT_DIR, "database", "indicator_toggles.json")


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)


def _init_schema() -> None:
    with _connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS market_pref (
                chat_id INTEGER PRIMARY KEY,
                market TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS mode_state (
                chat_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                is_on INTEGER NOT NULL DEFAULT 0,
                market TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, mode)
            )
            """
        )
        # --- history for the two "... - Status" buttons ---
        # alerts_log: one row per volume-spike alert actually SENT to a
        # chat (jobs/volume_spike_watcher.py). Persisted (unlike that
        # module's in-memory cooldown dict) so Status survives a bot
        # restart.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                scope TEXT NOT NULL,
                raw_symbol TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                pct_change REAL NOT NULL,
                last_price REAL,
                ts TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_alerts_log_chat ON alerts_log (chat_id, ts)")
        # scan_log: one row per scan ATTEMPT - either a strong_signal_watcher
        # tick (source="watcher") or a one-shot Search Signal run
        # (source="search"). status is "success"/"failed" so Status can
        # show a success/fail breakdown, not just a raw usage count.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                market TEXT NOT NULL,
                status TEXT NOT NULL,
                scanned_count INTEGER,
                error TEXT,
                ts TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_scan_log_chat ON scan_log (chat_id, ts)")
        # signals_log: one row per individual tradeable signal actually
        # shown/pushed to a chat, from either source above - what "last
        # 12 signals the system generated" reads from.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS signals_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                scope TEXT NOT NULL,
                symbol TEXT NOT NULL,
                verdict TEXT NOT NULL,
                confidence REAL,
                ts TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_signals_log_chat ON signals_log (chat_id, ts)")

        # --- pump/reversal tracking (engine/pump_tracker.py) ---
        # One row per (scope, raw_symbol, day) - a daily close snapshot
        # used to compute a trailing cumulative % move over several
        # days (a single ticker's 24h change can't see a multi-day
        # pump). Shared across all chats, like the price/volume history
        # in jobs/volume_spike_watcher.py - the market is the same for
        # everyone watching it.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS pump_price_history (
                scope TEXT NOT NULL,
                raw_symbol TEXT NOT NULL,
                day TEXT NOT NULL,
                close_price REAL NOT NULL,
                PRIMARY KEY (scope, raw_symbol, day)
            )
            """
        )
        # One row per (scope, raw_symbol) - the CURRENT overextension
        # state, if any. `resolved`=0 means still being watched for a
        # reversal; a fresh flag_overextended() call always resets it
        # to 0 (so a pair that pumps again after a resolved alert gets
        # re-armed automatically). `peak_price` only ever grows while
        # unresolved (see engine/pump_tracker.py).
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS overextended_pairs (
                scope TEXT NOT NULL,
                raw_symbol TEXT NOT NULL,
                symbol TEXT NOT NULL,
                cumulative_pct REAL NOT NULL,
                peak_price REAL NOT NULL,
                flagged_at TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (scope, raw_symbol)
            )
            """
        )


_init_schema()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- last Spot/Future/Both choice ---

def set_market_pref(chat_id: int, market: str) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO market_pref (chat_id, market, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET market = excluded.market, updated_at = excluded.updated_at",
            (chat_id, market, _now()),
        )


def get_market_pref(chat_id: int) -> str | None:
    with _connect() as con:
        row = con.execute(
            "SELECT market FROM market_pref WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row[0] if row else None


# --- per-mode ON/OFF (mode is "market_analyse" or "strong_signal") ---

def is_mode_on(chat_id: int, mode: str) -> bool:
    with _connect() as con:
        row = con.execute(
            "SELECT is_on FROM mode_state WHERE chat_id = ? AND mode = ?", (chat_id, mode)
        ).fetchone()
    return bool(row and row[0])


def set_mode_on(chat_id: int, mode: str, market: str) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO mode_state (chat_id, mode, is_on, market, updated_at) VALUES (?, ?, 1, ?, ?) "
            "ON CONFLICT(chat_id, mode) DO UPDATE SET is_on = 1, market = excluded.market, updated_at = excluded.updated_at",
            (chat_id, mode, market, _now()),
        )


def set_mode_off(chat_id: int, mode: str) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO mode_state (chat_id, mode, is_on, market, updated_at) VALUES (?, ?, 0, NULL, ?) "
            "ON CONFLICT(chat_id, mode) DO UPDATE SET is_on = 0, updated_at = excluded.updated_at",
            (chat_id, mode, _now()),
        )


def get_active_chats_for_mode(mode: str) -> list[tuple[int, str]]:
    """
    Every (chat_id, market) currently ON for `mode` - meant to be
    called once at bot startup (bot/main.py) so each chat's job_queue
    watcher gets re-scheduled after a restart, instead of an ON toggle
    silently dying the moment the bot process restarts.
    """
    with _connect() as con:
        rows = con.execute(
            "SELECT chat_id, market FROM mode_state WHERE mode = ? AND is_on = 1", (mode,)
        ).fetchall()
    return [(chat_id, market) for chat_id, market in rows]


def get_mode_info(chat_id: int, mode: str) -> dict:
    """
    {"is_on": bool, "market": str|None, "updated_at": str|None} for one
    chat+mode. `updated_at` doubles as "turned on since" - set_mode_on()
    above only ever writes it at the moment a mode is switched ON (or
    OFF), so while a mode stays ON this value doesn't move - it's
    exactly the "running since" timestamp the Status buttons need.
    """
    with _connect() as con:
        row = con.execute(
            "SELECT is_on, market, updated_at FROM mode_state WHERE chat_id = ? AND mode = ?",
            (chat_id, mode),
        ).fetchone()
    if not row:
        return {"is_on": False, "market": None, "updated_at": None}
    return {"is_on": bool(row[0]), "market": row[1], "updated_at": row[2]}


# --- Status button #1: "24/7 Market Analyse - Status" ---

def log_alert(chat_id: int, scope: str, raw_symbol: str, symbol: str, direction: str,
              pct_change: float, last_price: float | None) -> None:
    """Called by jobs/volume_spike_watcher.py right after an alert is actually sent."""
    with _connect() as con:
        con.execute(
            "INSERT INTO alerts_log (chat_id, scope, raw_symbol, symbol, direction, pct_change, last_price, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, scope, raw_symbol, symbol, direction, pct_change, last_price, _now()),
        )


def get_market_analyse_status(chat_id: int) -> dict:
    """
    Everything the "24/7 Market Analyse - Status" button shows: whether
    it's running, which market, how long it's been running, and a
    Spot/Future breakdown of alerts sent since it was turned on.
    """
    info = get_mode_info(chat_id, "market_analyse")
    since = info["updated_at"]

    with _connect() as con:
        row = con.execute(
            "SELECT scope, raw_symbol, symbol, direction, pct_change, last_price, ts "
            "FROM alerts_log WHERE chat_id = ? ORDER BY ts DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        last_alert = None
        if row:
            last_alert = {
                "scope": row[0], "rawSymbol": row[1], "symbol": row[2],
                "direction": row[3], "pctChange": row[4], "lastPrice": row[5], "ts": row[6],
            }

        counts = {"bitget-spot": 0, "bitget-futures": 0}
        count_rows = con.execute(
            "SELECT scope, COUNT(*) FROM alerts_log WHERE chat_id = ? AND ts >= COALESCE(?, '') GROUP BY scope",
            (chat_id, since),
        ).fetchall()
        for scope, n in count_rows:
            counts[scope] = n

        per_scope_last = {}
        for scope in ("bitget-spot", "bitget-futures"):
            r = con.execute(
                "SELECT symbol, direction, pct_change, ts FROM alerts_log "
                "WHERE chat_id = ? AND scope = ? ORDER BY ts DESC LIMIT 1",
                (chat_id, scope),
            ).fetchone()
            if r:
                per_scope_last[scope] = {"symbol": r[0], "direction": r[1], "pctChange": r[2], "ts": r[3]}

    return {
        "isOn": info["is_on"],
        "market": info["market"],
        "since": since,
        "lastAlert": last_alert,
        "spotAlertCount": counts["bitget-spot"],
        "futureAlertCount": counts["bitget-futures"],
        "lastSpotAlert": per_scope_last.get("bitget-spot"),
        "lastFutureAlert": per_scope_last.get("bitget-futures"),
    }


# --- Status button #2: "Find 24/7 Strong Signal - Status" ---

def log_scan(chat_id: int, source: str, market: str, status: str,
             scanned_count: int | None = None, error: str | None = None) -> None:
    """
    Called after every scan ATTEMPT - source is "watcher" (a
    strong_signal_watcher.py tick, whether or not it hit the shared
    scan cache) or "search" (a one-shot Search Signal run). status is
    "success" or "failed".
    """
    with _connect() as con:
        con.execute(
            "INSERT INTO scan_log (chat_id, source, market, status, scanned_count, error, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, source, market, status, scanned_count, error, _now()),
        )


def log_signal(chat_id: int, source: str, scope: str, symbol: str, verdict: str, confidence: float) -> None:
    """Called for every individual signal actually shown/pushed to a chat (either source)."""
    with _connect() as con:
        con.execute(
            "INSERT INTO signals_log (chat_id, source, scope, symbol, verdict, confidence, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, source, scope, symbol, verdict, confidence, _now()),
        )


def get_strong_signal_status(chat_id: int) -> dict:
    """
    Everything the "Find 24/7 Strong Signal - Status" button shows:
    whether the 24/7 watcher is running + since when, total scan usage
    (watcher ticks + one-shot Search Signal runs combined, and split
    out) with a success/fail breakdown, a Spot vs Future breakdown of
    how many signals were actually found, and the last 12 signals.
    """
    info = get_mode_info(chat_id, "strong_signal")

    with _connect() as con:
        scan_rows = con.execute(
            "SELECT source, status, COUNT(*) FROM scan_log WHERE chat_id = ? GROUP BY source, status",
            (chat_id,),
        ).fetchall()
        scan_counts = {"watcher": {"success": 0, "failed": 0}, "search": {"success": 0, "failed": 0}}
        for source, status, n in scan_rows:
            if source in scan_counts and status in scan_counts[source]:
                scan_counts[source][status] = n

        scope_rows = con.execute(
            "SELECT scope, COUNT(*) FROM signals_log WHERE chat_id = ? GROUP BY scope",
            (chat_id,),
        ).fetchall()
        scope_counts = {"bitget-spot": 0, "bitget-futures": 0}
        for scope, n in scope_rows:
            scope_counts[scope] = n

        last_rows = con.execute(
            "SELECT source, scope, symbol, verdict, confidence, ts FROM signals_log "
            "WHERE chat_id = ? ORDER BY ts DESC LIMIT 12",
            (chat_id,),
        ).fetchall()
        last_signals = [
            {"source": r[0], "scope": r[1], "symbol": r[2], "verdict": r[3], "confidence": r[4], "ts": r[5]}
            for r in last_rows
        ]

    return {
        "isOn": info["is_on"],
        "market": info["market"],
        "since": info["updated_at"],
        "watcherScans": scan_counts["watcher"],
        "searchScans": scan_counts["search"],
        "spotSignalCount": scope_counts["bitget-spot"],
        "futureSignalCount": scope_counts["bitget-futures"],
        "lastSignals": last_signals,
    }


# --- pump/reversal tracking (engine/pump_tracker.py) ---

def record_daily_price(scope: str, raw_symbol: str, price: float) -> None:
    """
    Upserts today's (UTC) close snapshot for one pair. Safe to call on
    every tick for every pair - same day's row just gets overwritten
    with the latest price seen that day, so by end-of-day it holds
    that day's last observed price.
    """
    day = datetime.now(timezone.utc).date().isoformat()
    with _connect() as con:
        con.execute(
            "INSERT INTO pump_price_history (scope, raw_symbol, day, close_price) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(scope, raw_symbol, day) DO UPDATE SET close_price = excluded.close_price",
            (scope, raw_symbol, day, price),
        )


def get_cumulative_pct(scope: str, raw_symbol: str, window_days: int) -> float | None:
    """
    % change from the OLDEST daily snapshot within the trailing
    `window_days` to the most recent one - approximates a multi-day
    cumulative pump that a single 24h ticker figure can't see. Returns
    None if there isn't at least 2 days of history yet for this pair
    (not enough to judge a "cumulative" move from).
    """
    with _connect() as con:
        rows = con.execute(
            "SELECT close_price FROM pump_price_history WHERE scope = ? AND raw_symbol = ? "
            "ORDER BY day DESC LIMIT ?",
            (scope, raw_symbol, window_days),
        ).fetchall()
    if len(rows) < 2:
        return None
    latest = rows[0][0]
    oldest = rows[-1][0]
    if not oldest:
        return None
    return (latest - oldest) / oldest * 100


def prune_pump_history(older_than_days: int) -> None:
    """Housekeeping - drops daily snapshots older than the window anything still cares about."""
    cutoff = (datetime.now(timezone.utc).date().toordinal() - older_than_days)
    with _connect() as con:
        con.execute(
            "DELETE FROM pump_price_history WHERE julianday(day) < julianday('now') - ?",
            (older_than_days,),
        )


def flag_overextended(scope: str, raw_symbol: str, symbol: str, cumulative_pct: float, price: float) -> None:
    """
    Marks (or re-arms) a pair as overextended and being watched for a
    reversal. `peak_price` only ever grows - it's the highest price
    seen since this pair was first flagged, which is what the reversal
    check measures the drop against.
    """
    with _connect() as con:
        con.execute(
            "INSERT INTO overextended_pairs (scope, raw_symbol, symbol, cumulative_pct, peak_price, flagged_at, resolved) "
            "VALUES (?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(scope, raw_symbol) DO UPDATE SET "
            "  symbol = excluded.symbol, "
            "  cumulative_pct = MAX(overextended_pairs.cumulative_pct, excluded.cumulative_pct), "
            "  peak_price = MAX(overextended_pairs.peak_price, excluded.peak_price), "
            "  resolved = 0",
            (scope, raw_symbol, symbol, cumulative_pct, price, _now()),
        )


def get_overextended(scope: str) -> list[dict]:
    """Every pair currently flagged & unresolved for `scope` - what strong_signal_watcher checks for a reversal each tick."""
    with _connect() as con:
        rows = con.execute(
            "SELECT raw_symbol, symbol, cumulative_pct, peak_price, flagged_at FROM overextended_pairs "
            "WHERE scope = ? AND resolved = 0",
            (scope,),
        ).fetchall()
    return [
        {"rawSymbol": r[0], "symbol": r[1], "cumulativePct": r[2], "peakPrice": r[3], "flaggedAt": r[4]}
        for r in rows
    ]


def resolve_overextended(scope: str, raw_symbol: str) -> None:
    """Called right after a reversal alert is sent for this pair, so it isn't re-alerted every tick while it keeps sliding."""
    with _connect() as con:
        con.execute(
            "UPDATE overextended_pairs SET resolved = 1 WHERE scope = ? AND raw_symbol = ?",
            (scope, raw_symbol),
        )


# --- indicator toggles (shipped defaults, database/indicator_toggles.json) ---

def get_enabled_indicators() -> dict | None:
    """
    {key: bool} map matching engine/indicators/analysis.py's
    INDICATOR_KEYS - passed straight through as scan_market()'s
    enabled_indicators argument. Returns None (= everything on, per
    compute_all_indicators()'s own default) if the file is missing or
    unparsable, rather than failing an entire scan over a config-file
    hiccup.
    """
    try:
        with open(INDICATOR_TOGGLES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.error(f"Could not load indicator_toggles.json, defaulting to all indicators on: {exc}")
        return None