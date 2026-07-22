# crypto-telegram-bot

Telegram bot version of the crypto-analyzer web app - same analysis
engine (indicators, trading concepts, signal scanner, Bitget adapter),
delivered through a Telegram bot instead of a browser dashboard.

## Build status

This project is complete - every file in the file plan (`bot/`,
`jobs/`, `database/`, `engine/`, `tests/`) is implemented and the bot
runs end to end via `python -m bot.main`.

## Setup

1. **Install Python 3.10+** if you don't have it already.

2. **Create a virtual environment (recommended):**
   ```
   python -m venv venv
   venv\Scripts\activate        # Windows
   source venv/bin/activate     # macOS/Linux
   ```

3. **Install dependencies:**
   ```
   pip install -r requirements.txt
   ```

4. **Get a Telegram bot token:**
   - Open a chat with [@BotFather](https://t.me/BotFather) on Telegram
   - Send `/newbot` and follow the prompts
   - Copy the token it gives you

5. **Set up your `.env` file:**
   - Copy `.env.example` to `.env` (or just edit the `.env` already in
     this drop)
   - Paste your bot token into `TELEGRAM_BOT_TOKEN=`
   - `ALPHA_VANTAGE_API_KEY` is optional - only powers one extra news
     source, everything else works without it

6. **Tune behaviour (optional):** `config/settings.yaml` holds every
   adjustable number - indicator periods, volume-spike thresholds,
   signal-confidence cutoffs, scan intervals, etc. Nothing in here is a
   secret, so it's safe to edit and commit.

## Running

```
python -m bot.main
```

## Deploying to Render.com

This repo includes a `render.yaml` blueprint, so Render can configure
most of this automatically:

1. Push this repo to GitHub (see the note in `.gitignore` below first -
   your real `.env` never gets committed, which is exactly what you want).
2. In the Render dashboard: **New -> Blueprint**, pick this GitHub repo.
   Render reads `render.yaml` and proposes the service - review and
   click **Apply**.
3. When prompted, paste your real `TELEGRAM_BOT_TOKEN` (and
   `ALPHA_VANTAGE_API_KEY` if you have one) - these are entered directly
   in Render's dashboard as environment variables, never through the repo.
4. Deploy. Render runs `pip install -r requirements.txt` then
   `python -m bot.main`.

**Two things to understand before relying on this in production**
(also documented inline in `render.yaml`):

- **Free tier sleeps.** Render's free Web Services spin down after
  ~15 minutes with no inbound HTTP traffic, and nothing ever calls this
  bot's health-check endpoint on its own - so a free instance *will*
  go to sleep and stop polling Telegram, breaking the "24/7" part
  entirely. For genuine always-on behaviour, either:
  - Upgrade to Render's **Starter** plan (`plan: starter` in
    `render.yaml`) - stays running continuously, no sleep, and unlocks
    persistent disks (see below); or
  - Stay on free and use an external uptime pinger (e.g.
    [UptimeRobot](https://uptimerobot.com)) to hit your Render URL
    every ~10 minutes. This works but isn't bulletproof (cold starts
    add a delay, and Render's free plan has a shared monthly hour cap
    across all your free services).

- **SQLite state resets on the free tier.** `database/bot_state.db`
  (who has which mode ON) lives on local disk. Render's filesystem is
  ephemeral on the free plan - every restart/redeploy wipes it, so
  users would need to re-toggle their modes afterward. This doesn't
  break anything, it just means state isn't remembered across
  restarts. If you're on the paid Starter plan, uncomment the `disk:`
  block in `render.yaml` to attach a persistent disk and this stops
  being a concern.

## Project layout

`bot/` - the Telegram-facing layer: keyboards, per-button handlers,
message formatting, and per-chat mode state (`state_store.py`,
backed by SQLite). `jobs/` - the two 24/7 background watchers
(`volume_spike_watcher.py`, `strong_signal_watcher.py`), each scheduled
per-chat via `python-telegram-bot`'s job queue when a user turns a mode
on. `engine/` - the analysis engine (indicators, trading concepts,
signal scanner, Bitget adapter, order flow, news), carried over from
the original web dashboard project. `database/` - the SQLite state
file plus the shipped default indicator on/off toggles. `tests/` -
sanity tests for the indicator engine.

## Notes

- `database/bot_state.db` (per-user toggle/mode state) and `logs/` are
  git-ignored - they're runtime data, not source.
- `database/indicator_toggles.json` IS committed - it's the shipped
  default indicator on/off state, not a runtime file.