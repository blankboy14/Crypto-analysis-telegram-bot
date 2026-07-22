"""
bot/scan_executor.py

ISOLATION FIX (issue #1 in what_i_neeed.txt: "24/7 Market Analyse",
"Find 24/7 Strong Signal", and "Search Signal" must never interfere
with each other).

By default, `loop.run_in_executor(None, ...)` hands blocking work to
asyncio's own DEFAULT executor - a shared ThreadPoolExecutor the whole
bot process draws from for every blocking call, everywhere. A full
market scan (engine/signal_scanner.py's run_full_scan/scan_market) is
explicitly a multi-minute, many-pair operation. If that ran on the
same default executor as everything else, a heavy Search Signal scan
or a Strong Signal watcher tick could starve that shared pool and
delay/stall unrelated quick tasks other chats or other modes need -
exactly the "one feature interferes with another" failure mode raised.

The fix: give scans their OWN small, dedicated, bounded thread pool,
completely separate from the default executor. Nothing else in the
bot should ever be submitted here except a full scan_market /
scan_market_above_confidence call.

MAX_CONCURRENT_SCANS = 3 sizing: the three FULL-SCAN call sites are
market_select.py's Search Signal run, and strong_signal_watcher.py's
per-market (spot/future/both) scan - at most 3 distinct markets can
ever be full-scanning at once (strong_signal_watcher.py's own lock
already guarantees only ONE scan per market is in flight, see that
module's docstring). A 4th caller (e.g. a second chat's Search Signal
landing at the same moment) simply queues behind these instead of
spawning a 4th concurrent scan - each scan internally already uses its
own worker_count-sized pool (default 8) for per-pair parallelism, so
uncapped scan-level concurrency here would multiply Bitget API request
volume fast enough to risk rate-limiting every scan at once.

single_pair_analyse.py also submits here (one analyze_one_pair() call
per pair, not a full scan) - much lighter than the three above, and
briefly sharing this same bounded pool is fine; it just queues behind
whatever's already running rather than getting its own pool.
"""
from concurrent.futures import ThreadPoolExecutor

MAX_CONCURRENT_SCANS = 3

SCAN_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_SCANS,
    thread_name_prefix="scan-executor",
)