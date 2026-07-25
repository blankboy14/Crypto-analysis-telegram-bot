# Keep-alive + status dashboard — setup

Two new files, drop them straight into your project (same folder
structure):

```
jobs/keepalive.py      <- new file
web/__init__.py        <- new file (empty)
web/dashboard.py       <- new file
```

Nothing else was touched. To actually turn them on, `bot/main.py`
needs 2 tiny edits — I did not make these for you since you asked for
files only, but they are copy-paste and low risk. Here they are,
exactly:

## Edit 1 — imports

Find this line near the top of `bot/main.py`:

```python
from jobs import heartbeat, signal_outcome_tracker
```

Replace it with:

```python
from jobs import heartbeat, keepalive, signal_outcome_tracker
from web.dashboard import start_dashboard_server
```

## Edit 2 — start it in `main()`

Find this near the bottom of `bot/main.py`:

```python
def main() -> None:
    settings = load_settings()
    configure_logging(settings)

    _start_health_server()

    application = build_application(settings)
```

Replace the `_start_health_server()` line with:

```python
    start_dashboard_server()
    keepalive.start()
```

That's the whole change — 3 lines added, 1 line swapped. You can leave
the old `_start_health_server` function sitting unused in the file, or
delete it; either is fine.

## New .env values (add to your `.env`, and to Render's dashboard env vars)

```
PUBLIC_URL=https://crypto-analysis-telegram-bot.onrender.com
KEEPALIVE_INTERVAL_MINUTES=10
KEEPALIVE_TELEGRAM_NOTIFY=true
```

- `PUBLIC_URL` — your Render URL. **Required** — without it the
  keep-alive silently does nothing (it logs why and the bot still runs
  fine, it just won't self-ping).
- `KEEPALIVE_INTERVAL_MINUTES` — how often it pings itself. 10 is safe
  (Render sleeps at 15 min idle). Don't go above 14.
- `KEEPALIVE_TELEGRAM_NOTIFY` — `true` sends a Telegram message on
  every check (~every 10 min, on top of your existing hourly
  heartbeat). Set to `false` if that's too many messages — the web
  dashboard still shows the same info either way, silently.

`requests` is already in your `requirements.txt`, so no dependency
changes needed.

## What you get

- Visit `https://crypto-analysis-telegram-bot.onrender.com` any time →
  a small dark dashboard showing current time, last check, next
  check, check/fail counts, and process uptime. Auto-refreshes every
  30s.
- `https://crypto-analysis-telegram-bot.onrender.com/status` → same
  data as JSON.
- Telegram messages (if enabled) confirming each check, to the same
  chat as your existing hourly heartbeat (`HEARTBEAT_CHAT_ID`).

## The honest limits (please read)

- This **prevents** sleep, it can't **undo** it — if the process is
  already down (asleep, crashed, redeploying), it can't ping itself
  awake, because it isn't running. Keeping the interval at 10 minutes
  is what stops that situation from happening in normal use.
- Render's free plan includes 750 instance-hours/month/workspace. One
  service running 24/7 uses roughly 730–745 hours/month, so this fits
  — as long as it's the *only* free service in that Render workspace.
  A second always-on free service in the same workspace could run out
  of hours before the month ends.
- Belt-and-suspenders option, free, no conflict with any of this: also
  point an external monitor (UptimeRobot, cron-job.org) at the same
  `PUBLIC_URL` every 5–10 min. Redundant pings do no harm.