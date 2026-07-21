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