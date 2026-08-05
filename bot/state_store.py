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
import random
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

        # Migration: message_text was added after this table already
        # shipped, so an existing database's signals_log won't have it
        # yet - ADD COLUMN it in if missing rather than requiring a
        # separate migration step (existing rows just get NULL, which
        # every reader below already falls back on gracefully).
        existing_cols = {row[1] for row in con.execute("PRAGMA table_info(signals_log)").fetchall()}
        if "message_text" not in existing_cols:
            con.execute("ALTER TABLE signals_log ADD COLUMN message_text TEXT")

        # Migration: batch_ts/scan_index let a multi-result push (e.g.
        # Search Signal's #1/#2/#3) be displayed in its ORIGINAL 1/2/3
        # order even though the rows were necessarily inserted one at a
        # time (so a plain "ORDER BY ts DESC" would show 3/2/1 within
        # each run). batch_ts is shared by every row from the same
        # push; scan_index is that row's position (#1, #2, #3...)
        # within it. Both NULL for older rows / single-result pushes -
        # get_strong_signal_status/get_search_signal_status already
        # fall back to that row's own ts as its batch when NULL.
        if "batch_ts" not in existing_cols:
            con.execute("ALTER TABLE signals_log ADD COLUMN batch_ts TEXT")
        if "scan_index" not in existing_cols:
            con.execute("ALTER TABLE signals_log ADD COLUMN scan_index INTEGER")

        # signal_outcomes: one row per tradeable signal that had a real
        # trade plan (Entry/SL/TP1-3) - tracked going forward by
        # jobs/signal_outcome_tracker.py, which polls price/candle data
        # and fills in what actually happened, so "did my signals
        # actually work" is answered by the bot itself instead of
        # someone tallying it up by hand. highest_tp_hit is the best
        # target reached SO FAR even if price later reverses back to
        # stop - that's what turns into a status like "tp1_then_sl"
        # rather than losing the fact that TP1 was genuinely reached
        # first. status starts "pending" and only ever moves forward
        # (never back) through: pending -> tp1_hit -> tp2_hit -> tp3_hit
        # (closed), or pending/tp1_hit/tp2_hit -> sl_hit (closed).
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                scope TEXT NOT NULL,
                raw_symbol TEXT NOT NULL,
                symbol TEXT NOT NULL,
                verdict TEXT NOT NULL,
                entry REAL NOT NULL,
                stop_loss REAL NOT NULL,
                tp1 REAL, tp2 REAL, tp3 REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                highest_tp_hit INTEGER NOT NULL DEFAULT 0,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                last_checked_at TEXT
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_signal_outcomes_status ON signal_outcomes (status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_signal_outcomes_chat ON signal_outcomes (chat_id, opened_at)")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_outcomes_pair ON signal_outcomes (scope, raw_symbol, status)"
        )

        # Migration: current_stop tracks the LIVE stop level, separate
        # from the original stop_loss - jobs/signal_outcome_tracker.py
        # moves this to breakeven/TP1 once a trade reaches TP1/TP2 (see
        # risk_management.move_stop_to_breakeven_after_tp1 in
        # settings.yaml) so a trade that already reached a target can no
        # longer fully round-trip back to the ORIGINAL stop. stop_loss
        # itself is left untouched as the historical record of what was
        # actually sent to the user. Existing rows default to their own
        # stop_loss (no trailing applied retroactively).
        existing_outcome_cols = {row[1] for row in con.execute("PRAGMA table_info(signal_outcomes)").fetchall()}
        if "current_stop" not in existing_outcome_cols:
            con.execute("ALTER TABLE signal_outcomes ADD COLUMN current_stop REAL")
            con.execute("UPDATE signal_outcomes SET current_stop = stop_loss WHERE current_stop IS NULL")

        # Migration: Trade ID / manual-activation system. Every signal
        # generated (Search Signal, Find 24/7 Strong Signal, Single
        # Pair Analyse) now gets a row here immediately with a unique
        # trade_id, but active stays 0 - jobs/signal_outcome_tracker.py
        # ignores it entirely until the person actually types that
        # trade_id into "Active a Trade" (bot/handlers/trade_information.py),
        # which flips active to 1 and starts real tracking. entry_status
        # gates SL/TP checking: a freshly-activated trade sits at
        # 'waiting' until price actually touches its entry level (since
        # activation can happen long after the signal was generated,
        # by which point price has usually moved away from entry) -
        # only once entry_status flips to 'arrived' does SL/TP tracking
        # begin, walking forward from entry_arrived_at rather than
        # opened_at.
        if "trade_id" not in existing_outcome_cols:
            con.execute("ALTER TABLE signal_outcomes ADD COLUMN trade_id TEXT")
            con.execute("ALTER TABLE signal_outcomes ADD COLUMN active INTEGER NOT NULL DEFAULT 0")
            con.execute("ALTER TABLE signal_outcomes ADD COLUMN entry_status TEXT NOT NULL DEFAULT 'waiting'")
            con.execute("ALTER TABLE signal_outcomes ADD COLUMN activated_at TEXT")
            con.execute("ALTER TABLE signal_outcomes ADD COLUMN entry_arrived_at TEXT")
            con.execute("ALTER TABLE signal_outcomes ADD COLUMN confidence REAL")
            con.execute("ALTER TABLE signal_outcomes ADD COLUMN scan_label TEXT")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_outcomes_trade_id ON signal_outcomes (trade_id)")

        # Migration: "List with Balance" - a trade activated this way
        # locks a slice of the Wallet Balance as margin at that exact
        # moment (balance_mode/margin_locked/leverage_used/
        # position_notional/quantity_locked are all set together, only
        # for that mode - a plain "Only List" trade leaves these NULL
        # and never touches the wallet balance at all).
        if "balance_mode" not in existing_outcome_cols:
            con.execute("ALTER TABLE signal_outcomes ADD COLUMN balance_mode TEXT NOT NULL DEFAULT 'list_only'")
            con.execute("ALTER TABLE signal_outcomes ADD COLUMN margin_locked REAL")
            con.execute("ALTER TABLE signal_outcomes ADD COLUMN leverage_used INTEGER")
            con.execute("ALTER TABLE signal_outcomes ADD COLUMN position_notional REAL")
            con.execute("ALTER TABLE signal_outcomes ADD COLUMN quantity_locked REAL")

        # --- Wallet Balance (Money Management add-on) ---
        # A number the user TYPES IN and the bot remembers per chat -
        # NOT a live account balance (this bot has no exchange API key
        # / account access anywhere, only public market data - see
        # engine/risk_manager.py's module docstring). Used to size
        # suggested positions/leverage on every signal.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_balance (
                chat_id INTEGER PRIMARY KEY,
                balance REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

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
                high_price REAL,
                low_price REAL,
                PRIMARY KEY (scope, raw_symbol, day)
            )
            """
        )
        existing_pump_cols = {row[1] for row in con.execute("PRAGMA table_info(pump_price_history)").fetchall()}
        if "high_price" not in existing_pump_cols:
            # Pre-intraday-peak-tracking version of this table (close
            # price only) - backfill high/low with whatever close_price
            # already has, so old rows don't read back as NULL; going
            # forward, record_daily_price keeps these as the TRUE
            # intraday running max/min instead of just the latest tick's
            # price (see that function's docstring for why this
            # mattered - a pair that pumps hard and partially reverts
            # within the same day was silently losing its peak here).
            con.execute("ALTER TABLE pump_price_history ADD COLUMN high_price REAL")
            con.execute("ALTER TABLE pump_price_history ADD COLUMN low_price REAL")
            con.execute("UPDATE pump_price_history SET high_price = close_price, low_price = close_price WHERE high_price IS NULL")
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

        # --- meme/alt coin move tracking (jobs/meme_move_watcher.py) ---
        # One row per (scope, raw_symbol) - the CURRENT up/down move
        # being tracked, separate from overextended_pairs above (that
        # table is specifically the pump-reversal SELL-call pipeline
        # with its own entry/SL/TP semantics; this one is the simpler
        # "still climbing/falling, checkpoint every N%, then flag if it
        # snaps back" watch, in both directions).
        #   direction         - "up" or "down": which side is being tracked.
        #   last_announced_pct - the last checkpoint % already pushed
        #                        (60/80/100/... or -40/-50/-60/...), so
        #                        the SAME checkpoint never gets pushed
        #                        twice.
        #   extreme_price     - highest price seen since direction="up"
        #                        started (or lowest, for "down") - what
        #                        the reversal pullback/bounce % is
        #                        measured against. Only ever moves
        #                        further in the extreme direction.
        #   reversal_announced - 1 once a pullback/bounce alert has
        #                        fired for the CURRENT extreme_price -
        #                        resets to 0 automatically whenever
        #                        extreme_price reaches a new high/low,
        #                        so a fresh peak/trough can still earn
        #                        its own fresh reversal alert later.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS meme_move_state (
                scope TEXT NOT NULL,
                raw_symbol TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                last_announced_pct REAL,
                extreme_price REAL NOT NULL,
                reversal_announced INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope, raw_symbol)
            )
            """
        )

        # --- RSI extreme checkpoint tracking (jobs/rsi_extreme_watcher.py) ---
        # One row per (scope, raw_symbol, timeframe) currently sitting in
        # extreme RSI territory on THAT timeframe - "high" (>=80,
        # stepping to 90/100 - overbought, can reverse down anytime) or
        # "low" (<=25, stepping to 20/15 - oversold, can reverse up
        # anytime). Tracked per-timeframe (not just per-pair) because
        # rsi_extreme_watch.timeframes checks more than one timeframe
        # independently (1h and 4h by default) - a pair can easily be
        # extreme on one and not the other, and each earns its own
        # checkpoint sequence. Same checkpoint-stepping idea as
        # meme_move_state above, just driven by the RSI reading itself
        # instead of cumulative price %. While a pair has ANY row here
        # (on any timeframe), jobs/high_alert_watcher.py's full-engine
        # scan also includes it (see that module) - a "high" row is
        # scanned looking for a SELL setup, a "low" row for a BUY setup.
        existing_rsi_alert_cols = {
            row[1] for row in con.execute("PRAGMA table_info(rsi_alert_state)").fetchall()
        }
        if existing_rsi_alert_cols and "timeframe" not in existing_rsi_alert_cols:
            # Pre-multi-timeframe version of this table (single
            # timeframe, no timeframe column at all) - the timeframe
            # column is now part of the PRIMARY KEY, which SQLite can't
            # add via ALTER TABLE. Safe to just drop and recreate: this
            # table only ever holds LIVE tracking state (what's
            # currently extreme right now), never history - losing it
            # just means any pair's checkpoint sequence restarts from
            # the first threshold next tick, not a real loss.
            con.execute("DROP TABLE rsi_alert_state")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS rsi_alert_state (
                scope TEXT NOT NULL,
                raw_symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                last_announced_level REAL,
                flagged_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope, raw_symbol, timeframe)
            )
            """
        )
        existing_rsi_alert_cols2 = {
            row[1] for row in con.execute("PRAGMA table_info(rsi_alert_state)").fetchall()
        }
        if "flagged_at" not in existing_rsi_alert_cols2:
            # Simple additive column - lets bot/handlers/high_alert_pairs.py
            # show "how long ago" for RSI-sourced pairs too, same as
            # pump-sourced ones already show via overextended_pairs.
            # flagged_at itself is only ever SET on a fresh streak (see
            # upsert_rsi_alert_state) and preserved across checkpoint
            # updates within that same streak, so it means "when this
            # streak started", not "when this row last changed".
            con.execute("ALTER TABLE rsi_alert_state ADD COLUMN flagged_at TEXT")


_init_schema()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_iso() -> str:
    """
    Public wrapper around the same UTC-ISO format _now() uses -
    for a caller that needs to tag several log_signal() rows from one
    single push (e.g. Search Signal's #1/#2/#3) with one shared
    batch_ts, generated once before the loop rather than once per row.
    """
    return _now()


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


# --- Wallet Balance (Money Management add-on) ---

def set_wallet_balance(chat_id: int, balance: float) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO wallet_balance (chat_id, balance, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET balance = excluded.balance, updated_at = excluded.updated_at",
            (chat_id, balance, _now()),
        )


def get_wallet_balance(chat_id: int) -> float | None:
    with _connect() as con:
        row = con.execute(
            "SELECT balance FROM wallet_balance WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row[0] if row else None


def adjust_wallet_balance(chat_id: int, delta: float) -> float:
    """
    Adds `delta` (positive or negative) to the current saved Wallet
    Balance - used by "List with Balance" activation (locks margin,
    delta is negative) and by jobs/signal_outcome_tracker.py crediting
    a closed live-balance trade's margin + P/L back (delta is margin
    + realized P/L, which can itself be negative on a loss). Returns
    the new balance. A chat with no saved balance yet is treated as 0
    before applying delta, same as get_wallet_balance() returning None
    is already treated as "no balance" everywhere else.
    """
    current = get_wallet_balance(chat_id) or 0.0
    new_balance = current + delta
    set_wallet_balance(chat_id, new_balance)
    return new_balance


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


def log_signal(chat_id: int, source: str, scope: str, symbol: str, verdict: str, confidence: float,
               message_text: str | None = None, batch_ts: str | None = None, scan_index: int | None = None) -> None:
    """
    Called for every individual signal actually shown/pushed to a chat
    (any source). `message_text` is the EXACT text that was actually
    sent for this signal - stored so the Status buttons can show the
    real, fully-formatted signal (Entry/SL/TP/Send Time and all)
    instead of reconstructing a plainer summary line from just the
    columns below.

    `batch_ts`/`scan_index` are for a caller pushing several results at
    once (e.g. Search Signal's #1/#2/#3) - pass the SAME batch_ts
    (from a single now_iso() call before the loop) and each row's own
    1-based scan_index, so the Status views can put them back in their
    original 1/2/3 order instead of the reverse-insertion order a
    plain ts sort would produce. Leave both None for a single-result
    push (watcher/single_pair) - each row then defaults to being its
    own one-row "batch".
    """
    ts = _now()
    with _connect() as con:
        con.execute(
            "INSERT INTO signals_log "
            "(chat_id, source, scope, symbol, verdict, confidence, message_text, batch_ts, scan_index, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, source, scope, symbol, verdict, confidence, message_text, batch_ts or ts, scan_index, ts),
        )


def next_signal_serial(chat_id: int, source: str = "watcher") -> int:
    """
    1-based serial number for the NEXT signal about to be pushed to
    this chat from `source` (e.g. "watcher" for Find 24/7 Strong
    Signal) - just (count of that chat+source's past rows in
    signals_log) + 1. Used purely for the human-readable "Trade Signal
    #N" label so the user can track how many signals they've actually
    received over time; it is NOT a database id and carries no other
    meaning.
    """
    with _connect() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM signals_log WHERE chat_id = ? AND source = ?",
            (chat_id, source),
        ).fetchone()
    return (row[0] if row else 0) + 1


def get_strong_signal_status(chat_id: int) -> dict:
    """
    Everything the "Find 24/7 Strong Signal - Status" button shows:
    whether the 24/7 watcher is running + since when, total scan usage
    (watcher ticks + one-shot Search Signal runs combined, and split
    out) with a success/fail breakdown, and - scoped to ONLY signals
    the 24/7 watcher itself actually pushed (source='watcher'), never
    Search Signal or Single Pair Analyse results - a Spot vs Future
    breakdown of how many it found plus the last 12 it sent, each with
    its full original message text.
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
            "SELECT scope, COUNT(*) FROM signals_log WHERE chat_id = ? AND source = 'watcher' GROUP BY scope",
            (chat_id,),
        ).fetchall()
        scope_counts = {"bitget-spot": 0, "bitget-futures": 0}
        for scope, n in scope_rows:
            scope_counts[scope] = n

        last_rows = con.execute(
            "SELECT scope, symbol, verdict, confidence, message_text, batch_ts, scan_index, ts FROM signals_log "
            "WHERE chat_id = ? AND source = 'watcher' ORDER BY ts DESC LIMIT 12",
            (chat_id,),
        ).fetchall()
        last_signals = [
            {"scope": r[0], "symbol": r[1], "verdict": r[2], "confidence": r[3], "messageText": r[4],
             "batchTs": r[5], "scanIndex": r[6], "ts": r[7]}
            for r in last_rows
        ]
        # Most-recent PUSH first, but each push's own #1/#2/#3 results
        # in their original ascending order (not reverse-insertion order).
        last_signals.sort(key=lambda r: (r["batchTs"] or r["ts"], -(r["scanIndex"] or 0)), reverse=True)

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


def get_search_signal_status(chat_id: int) -> dict:
    """
    Everything the "Search Signal - Status" button shows - the same
    shape of report as get_strong_signal_status() above, but scoped to
    ONLY one-shot Search Signal runs (source='search'). No isOn/market
    fields here since Search Signal isn't a 24/7 toggle - it's a
    one-shot action with nothing to be "on" or "off".
    """
    with _connect() as con:
        scan_rows = con.execute(
            "SELECT status, COUNT(*) FROM scan_log WHERE chat_id = ? AND source = 'search' GROUP BY status",
            (chat_id,),
        ).fetchall()
        scan_counts = {"success": 0, "failed": 0}
        for status, n in scan_rows:
            if status in scan_counts:
                scan_counts[status] = n

        scope_rows = con.execute(
            "SELECT scope, COUNT(*) FROM signals_log WHERE chat_id = ? AND source = 'search' GROUP BY scope",
            (chat_id,),
        ).fetchall()
        scope_counts = {"bitget-spot": 0, "bitget-futures": 0}
        for scope, n in scope_rows:
            scope_counts[scope] = n

        last_rows = con.execute(
            "SELECT scope, symbol, verdict, confidence, message_text, batch_ts, scan_index, ts FROM signals_log "
            "WHERE chat_id = ? AND source = 'search' ORDER BY ts DESC LIMIT 12",
            (chat_id,),
        ).fetchall()
        last_signals = [
            {"scope": r[0], "symbol": r[1], "verdict": r[2], "confidence": r[3], "messageText": r[4],
             "batchTs": r[5], "scanIndex": r[6], "ts": r[7]}
            for r in last_rows
        ]
        last_signals.sort(key=lambda r: (r["batchTs"] or r["ts"], -(r["scanIndex"] or 0)), reverse=True)

    return {
        "totalRuns": scan_counts["success"] + scan_counts["failed"],
        "successRuns": scan_counts["success"],
        "failedRuns": scan_counts["failed"],
        "spotSignalCount": scope_counts["bitget-spot"],
        "futureSignalCount": scope_counts["bitget-futures"],
        "lastSignals": last_signals,
    }


# --- pump/reversal tracking (engine/pump_tracker.py) ---

def record_daily_price(scope: str, raw_symbol: str, price: float) -> None:
    """
    Upserts today's (UTC) price info for one pair. Safe to call on
    every tick for every pair. `close_price` is just the latest price
    seen today (unchanged behavior - used by get_cumulative_pct's
    "long-run drift" reading). `high_price`/`low_price` are the TRUE
    running max/min seen today across every tick, not overwritten -
    this is what get_peak_cumulative_pct uses, and it's the fix for a
    real bug: a pair that pumps 100%+ and then partially reverts
    WITHIN THE SAME DAY used to silently lose that peak, since the old
    version of this function only kept whatever price the LAST tick of
    the day happened to see - by which point a fast pump-and-dump could
    already be back down, making the pair never actually cross the
    overextended threshold even though it clearly should have.
    """
    day = datetime.now(timezone.utc).date().isoformat()
    with _connect() as con:
        con.execute(
            "INSERT INTO pump_price_history (scope, raw_symbol, day, close_price, high_price, low_price) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope, raw_symbol, day) DO UPDATE SET "
            "  close_price = excluded.close_price, "
            "  high_price = MAX(pump_price_history.high_price, excluded.high_price), "
            "  low_price = MIN(pump_price_history.low_price, excluded.low_price)",
            (scope, raw_symbol, day, price, price, price),
        )


def get_cumulative_pct(scope: str, raw_symbol: str, window_days: int) -> float | None:
    """
    % change from the OLDEST daily CLOSE snapshot within the trailing
    `window_days` to the most recent one - approximates a multi-day
    cumulative drift (up or down) that a single 24h ticker figure
    can't see. Returns None if there isn't at least 2 days of history
    yet for this pair. Note: this uses closes only, so it can
    understate a pair that spiked and partially reverted within a
    single day - see get_peak_cumulative_pct for that case.
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


def get_peak_cumulative_pct(scope: str, raw_symbol: str, window_days: int) -> tuple[float, float] | None:
    """
    Like get_cumulative_pct, but measured against the TRUE peak (for an
    up move) or trough (for a down move) reached anywhere within the
    window - not just the most recent day's closing snapshot. Returns
    (pct, extreme_price) using whichever of high_price/low_price
    produces the LARGER-magnitude move from the oldest day's close, or
    None if there isn't at least 2 days of history yet.

    This is what jobs/strong_signal_watcher.py's overextended-flagging
    check uses (get_cumulative_pct alone isn't enough there) - a pair
    that spikes 100%+ intraday and has already partially reverted by
    the time a tick runs would otherwise never cross the 80% threshold
    at all, since get_cumulative_pct only ever sees each day's LATEST
    price, not its peak.
    """
    with _connect() as con:
        rows = con.execute(
            "SELECT close_price, high_price, low_price FROM pump_price_history "
            "WHERE scope = ? AND raw_symbol = ? ORDER BY day DESC LIMIT ?",
            (scope, raw_symbol, window_days),
        ).fetchall()
    if len(rows) < 2:
        return None
    oldest_close = rows[-1][0]
    if not oldest_close:
        return None

    highs = [r[1] for r in rows if r[1] is not None]
    lows = [r[2] for r in rows if r[2] is not None]
    peak_high = max(highs) if highs else rows[0][0]
    peak_low = min(lows) if lows else rows[0][0]

    up_pct = (peak_high - oldest_close) / oldest_close * 100
    down_pct = (peak_low - oldest_close) / oldest_close * 100

    if abs(up_pct) >= abs(down_pct):
        return up_pct, peak_high
    return down_pct, peak_low


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


def get_meme_move_state(scope: str, raw_symbol: str) -> dict | None:
    """Current up/down move-tracking state for one pair, or None if it's not currently in either direction's tracked zone."""
    with _connect() as con:
        row = con.execute(
            "SELECT direction, last_announced_pct, extreme_price, reversal_announced FROM meme_move_state "
            "WHERE scope = ? AND raw_symbol = ?",
            (scope, raw_symbol),
        ).fetchone()
    if row is None:
        return None
    return {
        "direction": row[0], "lastAnnouncedPct": row[1], "extremePrice": row[2],
        "reversalAnnounced": bool(row[3]),
    }


def upsert_meme_move_state(scope: str, raw_symbol: str, symbol: str, direction: str,
                            last_announced_pct: float | None, extreme_price: float,
                            reversal_announced: bool) -> None:
    """Overwrites (or creates) this pair's move-tracking row - callers always pass the FULL new state, not a delta, so a stale row from a previous direction never leaks into the new one."""
    with _connect() as con:
        con.execute(
            "INSERT INTO meme_move_state (scope, raw_symbol, symbol, direction, last_announced_pct, extreme_price, reversal_announced, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope, raw_symbol) DO UPDATE SET "
            "  symbol = excluded.symbol, direction = excluded.direction, "
            "  last_announced_pct = excluded.last_announced_pct, extreme_price = excluded.extreme_price, "
            "  reversal_announced = excluded.reversal_announced, updated_at = excluded.updated_at",
            (scope, raw_symbol, symbol, direction, last_announced_pct, extreme_price, int(reversal_announced), _now()),
        )


def clear_meme_move_state(scope: str, raw_symbol: str) -> None:
    """Drops this pair's tracked move entirely - called once its cumulative % move settles back into the neutral zone (between the down and up thresholds), so the NEXT pump or dump starts fresh from the first checkpoint again instead of resuming mid-sequence."""
    with _connect() as con:
        con.execute("DELETE FROM meme_move_state WHERE scope = ? AND raw_symbol = ?", (scope, raw_symbol))


def get_rsi_alert_state(scope: str, raw_symbol: str, timeframe: str) -> dict | None:
    """Current RSI-extreme tracking state for one pair ON ONE TIMEFRAME, or None if it's not currently past either threshold on that timeframe."""
    with _connect() as con:
        row = con.execute(
            "SELECT direction, last_announced_level, flagged_at FROM rsi_alert_state "
            "WHERE scope = ? AND raw_symbol = ? AND timeframe = ?",
            (scope, raw_symbol, timeframe),
        ).fetchone()
    if row is None:
        return None
    return {"direction": row[0], "lastAnnouncedLevel": row[1], "flaggedAt": row[2]}


def upsert_rsi_alert_state(scope: str, raw_symbol: str, symbol: str, timeframe: str, direction: str,
                            last_announced_level: float | None) -> None:
    """Overwrites (or creates) this pair's RSI-extreme row for this ONE timeframe - callers always pass the FULL new state, same reasoning as upsert_meme_move_state. flagged_at is set only on a brand-new row (INSERT) and left untouched on every later checkpoint update (ON CONFLICT) within that same streak - it means "when this streak started", not "when this row last changed"."""
    now = _now()
    with _connect() as con:
        con.execute(
            "INSERT INTO rsi_alert_state (scope, raw_symbol, timeframe, symbol, direction, last_announced_level, flagged_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope, raw_symbol, timeframe) DO UPDATE SET "
            "  symbol = excluded.symbol, direction = excluded.direction, "
            "  last_announced_level = excluded.last_announced_level, updated_at = excluded.updated_at",
            (scope, raw_symbol, timeframe, symbol, direction, last_announced_level, now, now),
        )


def clear_rsi_alert_state(scope: str, raw_symbol: str, timeframe: str) -> None:
    """Drops this pair's RSI-extreme row for this ONE timeframe - called once RSI on that timeframe comes back inside the neutral band. A pair still extreme on another timeframe keeps that OTHER row (and stays in the High Alert pool through it)."""
    with _connect() as con:
        con.execute(
            "DELETE FROM rsi_alert_state WHERE scope = ? AND raw_symbol = ? AND timeframe = ?",
            (scope, raw_symbol, timeframe),
        )


def get_rsi_alert_pairs(scope: str) -> list[dict]:
    """Every (pair, timeframe) currently flagged in `scope` - what jobs/high_alert_watcher.py folds into its full-engine scan pool alongside get_overextended(). A pair extreme on more than one timeframe appears once per timeframe here; the caller dedupes by rawSymbol since only one full-engine scan per pair is needed regardless of how many timeframes flagged it."""
    with _connect() as con:
        rows = con.execute(
            "SELECT raw_symbol, symbol, direction, timeframe, flagged_at FROM rsi_alert_state WHERE scope = ?",
            (scope,),
        ).fetchall()
    return [{"rawSymbol": r[0], "symbol": r[1], "direction": r[2], "timeframe": r[3], "flaggedAt": r[4]} for r in rows]


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

# --- Signal Outcome Tracking (jobs/signal_outcome_tracker.py) ---

def _generate_unique_trade_id(con) -> str:
    """
    10-digit numeric Trade ID. Drawn from ONE shared pool across every
    source (Search Signal, Find 24/7 Strong Signal, Single Pair
    Analyse) rather than one counter per source, so two IDs can never
    collide with each other no matter where they came from.
    """
    for _ in range(50):
        candidate = str(random.randint(10 ** 9, 10 ** 10 - 1))  # always 10 digits, never leading-zero
        exists = con.execute("SELECT 1 FROM signal_outcomes WHERE trade_id = ?", (candidate,)).fetchone()
        if not exists:
            return candidate
    raise RuntimeError("Could not generate a unique trade ID after 50 attempts")


def record_signal_outcome_tracking(chat_id: int, source: str, scope: str, raw_symbol: str, symbol: str,
                                    verdict: str, entry: float, stop_loss: float,
                                    tp1: float | None, tp2: float | None, tp3: float | None,
                                    confidence: float | None = None, scan_label: str | None = None) -> str:
    """
    Called right alongside log_signal() for any signal that had a real
    trade plan. Unlike before, this no longer starts tracking it right
    away - it just reserves a unique trade_id and stores the plan with
    active=0. jobs/signal_outcome_tracker.py ignores it until the
    person activates that trade_id via "Active a Trade". Returns the
    trade_id so the caller can print it on the signal message itself.
    """
    with _connect() as con:
        trade_id = _generate_unique_trade_id(con)
        con.execute(
            "INSERT INTO signal_outcomes "
            "(chat_id, source, scope, raw_symbol, symbol, verdict, entry, stop_loss, current_stop, tp1, tp2, tp3, "
            "status, highest_tp_hit, opened_at, trade_id, active, entry_status, confidence, scan_label) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, 0, 'waiting', ?, ?)",
            (chat_id, source, scope, raw_symbol, symbol, verdict, entry, stop_loss, stop_loss, tp1, tp2, tp3,
             _now(), trade_id, confidence, scan_label),
        )
    return trade_id


def activate_trade(chat_id: int, trade_id: str) -> dict | None:
    """
    "Active a Trade" - looks up trade_id SCOPED TO THIS CHAT (a trade
    ID from a different chat is never activatable here) and flips it
    on for the tracker job to start watching. Returns the trade as a
    dict (with alreadyActive telling the caller whether this call is
    what just activated it, or it was already active before), or None
    if no such trade_id exists for this chat at all.
    """
    with _connect() as con:
        row = con.execute(
            "SELECT id, active, source, scope, raw_symbol, symbol, verdict, entry, stop_loss, tp1, tp2, tp3, "
            "confidence, scan_label, opened_at, trade_id FROM signal_outcomes WHERE chat_id = ? AND trade_id = ?",
            (chat_id, trade_id),
        ).fetchone()
        if row is None:
            return None
        already_active = bool(row[1])
        if not already_active:
            con.execute(
                "UPDATE signal_outcomes SET active = 1, activated_at = ? WHERE id = ?",
                (_now(), row[0]),
            )
    return {
        "id": row[0], "alreadyActive": already_active, "source": row[2], "scope": row[3], "rawSymbol": row[4],
        "symbol": row[5], "verdict": row[6], "entry": row[7], "stopLoss": row[8], "tp1": row[9], "tp2": row[10],
        "tp3": row[11], "confidence": row[12], "scanLabel": row[13], "openedAt": row[14], "tradeId": row[15],
    }


def set_trade_balance_mode(chat_id: int, trade_id: str, margin_locked: float, leverage: int,
                            position_notional: float, quantity: float) -> bool:
    """
    "List with Balance" - locks this trade to a slice of the Wallet
    Balance at the moment of choosing it (see bot/handlers/
    trade_information.py). Only ever called right after activate_trade()
    for a trade that's still 'list_only' - returns False if the trade
    isn't found for this chat or was already switched to 'list_with_balance'
    before (so a double-tap on the button can't lock margin twice).
    """
    with _connect() as con:
        cur = con.execute(
            "UPDATE signal_outcomes SET balance_mode = 'list_with_balance', margin_locked = ?, "
            "leverage_used = ?, position_notional = ?, quantity_locked = ? "
            "WHERE chat_id = ? AND trade_id = ? AND balance_mode = 'list_only'",
            (margin_locked, leverage, position_notional, quantity, chat_id, trade_id),
        )
        return cur.rowcount > 0


def get_trade_summary(chat_id: int) -> dict:
    """
    Counts for the 'Trade Information' button. totalActive only
    counts trades STILL OPEN (not yet sl_hit/tp3_hit) - a closed trade
    stops counting as "active" the moment it closes, same moment it
    drops off "See Last 12 Trade". touchSl / touchTp3 are running
    historical tallies (every SL/TP3 close ever, including ones that
    have since rolled off the last-12 list) - they never go back down.
    The three tpN_reversed_sl counts are a breakdown of touchSl: how
    many of those SL closes happened AFTER price had already reached
    TP1 / TP2 / TP3 (a "Down" close, not a clean stop-out). tp3RefersSl
    will structurally always read 0 under the current settings -
    reaching TP3 closes a trade immediately as a full win, so there's
    no further tracking left for it to later reverse into an SL hit.
    """
    with _connect() as con:
        rows = con.execute(
            "SELECT entry_status, status, highest_tp_hit FROM signal_outcomes WHERE chat_id = ? AND active = 1",
            (chat_id,),
        ).fetchall()
    return {
        "totalActive": sum(1 for r in rows if r[1] not in ("sl_hit", "tp3_hit")),
        "touchEntry": sum(1 for r in rows if r[0] == "arrived"),
        "touchSl": sum(1 for r in rows if r[1] == "sl_hit"),
        "touchTp1": sum(1 for r in rows if r[2] >= 1),
        "touchTp2": sum(1 for r in rows if r[2] >= 2),
        "touchTp3": sum(1 for r in rows if r[2] >= 3),
        "tp1RefersSl": sum(1 for r in rows if r[1] == "sl_hit" and r[2] == 1),
        "tp2RefersSl": sum(1 for r in rows if r[1] == "sl_hit" and r[2] == 2),
        "tp3RefersSl": sum(1 for r in rows if r[1] == "sl_hit" and r[2] == 3),
    }


_TRADE_ROW_COLUMNS = (
    "id, source, scope, raw_symbol, symbol, verdict, entry, stop_loss, current_stop, tp1, tp2, tp3, "
    "status, highest_tp_hit, entry_status, confidence, scan_label, opened_at, trade_id, "
    "balance_mode, margin_locked, leverage_used, position_notional, quantity_locked"
)


def _row_to_trade_dict(r) -> dict:
    return {
        "id": r[0], "source": r[1], "scope": r[2], "rawSymbol": r[3], "symbol": r[4], "verdict": r[5],
        "entry": r[6], "stopLoss": r[7], "currentStop": r[8] if r[8] is not None else r[7],
        "tp1": r[9], "tp2": r[10], "tp3": r[11], "status": r[12], "highestTpHit": r[13],
        "entryStatus": r[14], "confidence": r[15], "scanLabel": r[16], "openedAt": r[17], "tradeId": r[18],
        "balanceMode": r[19], "marginLocked": r[20], "leverageUsed": r[21],
        "positionNotional": r[22], "quantityLocked": r[23],
    }


def get_last_active_trades(chat_id: int, limit: int = 12) -> list:
    """
    'See Last 12 Trade' - most recently activated trades for this chat
    that are STILL OPEN, newest first. The moment a trade's SL or TP3
    hits (status becomes 'sl_hit'/'tp3_hit'), it's closed and drops out
    of this list entirely - it still counts permanently in
    get_trade_summary()'s touchSl/touchTp3 tallies, it just isn't
    browsable here anymore.
    """
    with _connect() as con:
        rows = con.execute(
            f"SELECT {_TRADE_ROW_COLUMNS} FROM signal_outcomes "
            "WHERE chat_id = ? AND active = 1 AND status NOT IN ('sl_hit', 'tp3_hit') "
            "ORDER BY activated_at DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [_row_to_trade_dict(r) for r in rows]


def get_trade_by_id_for_chat(chat_id: int, trade_id: str) -> dict | None:
    """'See Active Trade By ID' - only matches a trade that's active=1 AND still open for this chat (closed trades, and never-activated trade_ids, won't be found here)."""
    with _connect() as con:
        row = con.execute(
            f"SELECT {_TRADE_ROW_COLUMNS} FROM signal_outcomes "
            "WHERE chat_id = ? AND trade_id = ? AND active = 1 AND status NOT IN ('sl_hit', 'tp3_hit')",
            (chat_id, trade_id),
        ).fetchone()
    return _row_to_trade_dict(row) if row else None


def remove_trade(chat_id: int, trade_id: str) -> bool:
    """'Remove Trade' - deletes an activated trade outright (stops tracking it, drops it from every Trade Information view). Only ever touches a trade belonging to THIS chat. Returns False if no active trade with that ID exists for this chat."""
    with _connect() as con:
        cur = con.execute(
            "DELETE FROM signal_outcomes WHERE chat_id = ? AND trade_id = ? AND active = 1",
            (chat_id, trade_id),
        )
        return cur.rowcount > 0


def mark_entry_arrived(outcome_id: int, arrived_at_iso: str) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE signal_outcomes SET entry_status = 'arrived', entry_arrived_at = ?, last_checked_at = ? "
            "WHERE id = ?",
            (arrived_at_iso, _now(), outcome_id),
        )


def get_open_signal_outcomes(limit: int = 500) -> list:
    """
    Every ACTIVATED outcome row not yet fully closed (status is
    'pending', 'tp1_hit', or 'tp2_hit' - NOT 'tp3_hit'/'sl_hit', which
    are terminal) across ALL chats. active = 1 is the key filter now -
    a signal that was generated but never activated via "Active a
    Trade" is never picked up here, no matter how long ago it was sent.
    """
    with _connect() as con:
        rows = con.execute(
            "SELECT id, chat_id, source, scope, raw_symbol, symbol, verdict, entry, stop_loss, current_stop, "
            "tp1, tp2, tp3, status, highest_tp_hit, opened_at, trade_id, entry_status, activated_at, "
            "entry_arrived_at, balance_mode, margin_locked, leverage_used, position_notional FROM signal_outcomes "
            "WHERE active = 1 AND status NOT IN ('tp3_hit', 'sl_hit') ORDER BY opened_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "id": r[0], "chatId": r[1], "source": r[2], "scope": r[3], "rawSymbol": r[4], "symbol": r[5],
            "verdict": r[6], "entry": r[7], "stopLoss": r[8], "currentStop": r[9] if r[9] is not None else r[8],
            "tp1": r[10], "tp2": r[11], "tp3": r[12], "status": r[13], "highestTpHit": r[14], "openedAt": r[15],
            "tradeId": r[16], "entryStatus": r[17], "activatedAt": r[18], "entryArrivedAt": r[19],
            "balanceMode": r[20], "marginLocked": r[21], "leverageUsed": r[22], "positionNotional": r[23],
        }
        for r in rows
    ]


def update_signal_outcome(outcome_id: int, status: str, highest_tp_hit: int, closed: bool = False,
                           new_current_stop: float | None = None) -> None:
    """
    Applies a new status (and, if this closes it, closed_at) after
    jobs/signal_outcome_tracker.py checks price data. `new_current_stop`,
    when given, is the stop-loss trailing move (e.g. to breakeven after
    TP1) applied THIS check - persisted so the next check compares
    against the moved stop, not the original one forever.
    """
    now = _now()
    with _connect() as con:
        if closed:
            if new_current_stop is not None:
                con.execute(
                    "UPDATE signal_outcomes SET status = ?, highest_tp_hit = ?, current_stop = ?, "
                    "closed_at = ?, last_checked_at = ? WHERE id = ?",
                    (status, highest_tp_hit, new_current_stop, now, now, outcome_id),
                )
            else:
                con.execute(
                    "UPDATE signal_outcomes SET status = ?, highest_tp_hit = ?, closed_at = ?, last_checked_at = ? "
                    "WHERE id = ?",
                    (status, highest_tp_hit, now, now, outcome_id),
                )
        else:
            if new_current_stop is not None:
                con.execute(
                    "UPDATE signal_outcomes SET status = ?, highest_tp_hit = ?, current_stop = ?, "
                    "last_checked_at = ? WHERE id = ?",
                    (status, highest_tp_hit, new_current_stop, now, outcome_id),
                )
            else:
                con.execute(
                    "UPDATE signal_outcomes SET status = ?, highest_tp_hit = ?, last_checked_at = ? WHERE id = ?",
                    (status, highest_tp_hit, now, outcome_id),
                )


def touch_signal_outcome_checked(outcome_id: int) -> None:
    """No level crossed this tick - just record that it WAS checked, so a stuck/erroring pair is still visible as alive."""
    with _connect() as con:
        con.execute("UPDATE signal_outcomes SET last_checked_at = ? WHERE id = ?", (_now(), outcome_id))


def get_signal_outcome_stats(chat_id: int) -> dict:
    """
    Everything the "📊 Signal Outcomes" button shows for one chat - the
    exact breakdown a person would otherwise have to tally up by hand
    (SL hit / TP1 / TP2 / TP3 / TP-then-reversed / still running), plus
    a simple win rate off of it.
    """
    with _connect() as con:
        rows = con.execute(
            "SELECT status, highest_tp_hit, COUNT(*) FROM signal_outcomes "
            "WHERE chat_id = ? GROUP BY status, highest_tp_hit",
            (chat_id,),
        ).fetchall()

    counts = {
        "pending": 0, "tp1_hit": 0, "tp2_hit": 0,  # still open, sitting at this stage
        "tp3_hit": 0,  # closed - full target reached
        "sl_hit_clean": 0,  # closed - SL hit having never reached any TP
        "sl_hit_after_tp1": 0, "sl_hit_after_tp2": 0,  # closed - reached a TP, then reversed all the way back to SL
    }
    for status, highest_tp_hit, n in rows:
        if status == "sl_hit":
            if highest_tp_hit >= 2:
                counts["sl_hit_after_tp2"] += n
            elif highest_tp_hit >= 1:
                counts["sl_hit_after_tp1"] += n
            else:
                counts["sl_hit_clean"] += n
        elif status in counts:
            counts[status] += n

    total_closed = (
        counts["tp3_hit"] + counts["sl_hit_clean"] + counts["sl_hit_after_tp1"] + counts["sl_hit_after_tp2"]
    )
    total_open = counts["pending"] + counts["tp1_hit"] + counts["tp2_hit"]
    # "Win" = reached at least TP1 before anything else, even if it
    # later gave some back to SL - matches how the person doing this by
    # hand would count it (TP1 reached is still a real win, distinct
    # from a clean stop-out that never worked at all).
    wins = counts["tp3_hit"] + counts["sl_hit_after_tp1"] + counts["sl_hit_after_tp2"]
    win_rate = (wins / total_closed * 100) if total_closed else None

    return {**counts, "totalClosed": total_closed, "totalOpen": total_open, "winRate": win_rate}
