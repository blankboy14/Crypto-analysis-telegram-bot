# Crypto Signal Telegram Bot

A Telegram bot that scans crypto markets (via Bitget's public market-data
API) and pushes trading signals to Telegram — either on demand ("Search
Signal"), continuously in the background ("24/7" modes), or for a single
pair you name. Signals combine classic indicators (RSI, EMA, MACD,
Bollinger, Support/Resistance) with a large library of "smart money"
trading concepts (SMC, ICT, Wyckoff, Elliott Wave, order blocks, fair
value gaps, liquidity sweeps, market structure, etc.), then attach a
money-management plan (entry/SL/TP + position size) sized off your
wallet balance.

This README covers: what the bot actually does, how to run it locally
in VS Code, and how to deploy it on Render.com (free tier included).

---

## 1. What the bot does (feature by feature)

Everything below is a button on the bot's main menu
(`bot/keyboards.py`). Each one calls into `bot/handlers/`.

| Button | What it does |
|---|---|
| 📊 **24/7 Market Analyse** (On/Off/Status) | Background per-chat job that continuously watches your chosen market (Spot/Future/Both) and pushes alerts — volume spikes, absolute-volume bursts, daily movers, volatility-tier changes. See `jobs/volume_spike_watcher.py`. |
| 🔥 **Find 24/7 Strong Signal** (On/Off/Status) | Background per-chat job that re-scans the whole market on an interval (default every 15 min) and only pushes a signal when confidence clears a threshold (default 80). See `jobs/strong_signal_watcher.py`. |
| 🔎 **Search Signal** | One-shot: scans the market right now, shows the top 3 tradeable setups by confidence, then stops. Optional "Full Analysis" mode also sends a per-pair breakdown as the scan runs. See `bot/handlers/search_signal.py` + `market_select.py`. |
| 📊 **Signal Outcomes** | Shows how your past signals actually performed (hit TP1/TP2/TP3/SL or still open). Auto-checked every 5 min in the background — `jobs/signal_outcome_tracker.py`. |
| 🎯 **Single Pair Analyse** | Type any pair name (e.g. `BTCUSDT`) and get a full analysis for just that one. |
| 📋 **Market Details** | Browse raw market data (all pairs / gainers / losers / top by volume) without running a full signal scan. |
| 💰 **Wallet Balance** | Set the balance the bot uses to size positions in every money-management block it shows you. |
| ℹ️ **Help** | In-bot explanation of every button. |

**Under the hood, always running:**
- **Heartbeat** (`jobs/heartbeat.py`) — hourly Telegram message to `HEARTBEAT_CHAT_ID` proving the process is alive, plus a snapshot of what's running.
- **Keepalive** (`jobs/keepalive.py`) — self-pings the bot's own Render URL every ~10 min so a free-tier instance doesn't spin down.
- **Status dashboard** (`web/dashboard.py`) — visit the bot's public URL in a browser for a live status page (`/` = HTML, `/status` = JSON).

---

## 2. Project structure

```
crypto-telegram-bot/
├── bot/
│   ├── main.py            <- entry point (python -m bot.main)
│   ├── handlers/          <- one file per menu button / conversation step
│   ├── keyboards.py        <- main menu button labels
│   ├── formatters.py       <- turns raw scan results into the Telegram messages you see
│   ├── state_store.py      <- SQLite (database/bot_state.db): per-chat modes, wallet balance, signal history
│   └── scan_executor.py    <- dedicated thread pool so a heavy scan can't block anything else
├── engine/
│   ├── bitget_api.py       <- Bitget PUBLIC market-data API calls (no API key needed)
│   ├── signal_scanner.py   <- runs a full market scan across all pairs
│   ├── signal_engine.py    <- turns indicator/concept output into a BUY/SELL/verdict + confidence
│   ├── risk_manager.py     <- entry/SL/TP + position sizing
│   ├── indicators/         <- RSI, EMA, MACD, Bollinger, Support/Resistance, VWAP
│   ├── trading_concepts/   <- SMC, ICT, Wyckoff, Elliott Wave, order blocks, FVG, liquidity, market structure...
│   ├── news_service.py     <- optional news headlines (Alpha Vantage)
│   ├── order_flow.py, futures_metrics.py, exchange_adapter.py, http_client.py
├── jobs/
│   ├── heartbeat.py, keepalive.py, signal_outcome_tracker.py
│   ├── strong_signal_watcher.py, volume_spike_watcher.py
├── web/
│   └── dashboard.py         <- health check + status page (also what keeps Render's free web-service check happy)
├── config/
│   └── settings.yaml        <- every tunable number in the bot (thresholds, intervals, risk %, etc.)
├── database/
│   ├── bot_state.db         <- created automatically on first run (SQLite, not committed to git)
│   └── indicator_toggles.json
├── logs/                     <- one timestamped .txt file per run (git-ignored)
├── tests/
├── requirements.txt
├── render.yaml               <- Render Blueprint (optional, see §4)
└── .env                       <- you create this locally (see §3) — never commit it
```

---

## 3. Running locally in VS Code

### 3.1 Prerequisites
- **Python 3.12+** (the project pins `python-telegram-bot>=22.4`, which is
  what makes Python 3.14 work too — earlier PTB versions crash on 3.14).
- A Telegram bot token from **[@BotFather](https://t.me/BotFather)**
  (`/newbot` → copy the token it gives you).
- VS Code with the **Python extension** (ms-python.python) installed.

### 3.2 Setup

```bash
# 1. Open the project folder in VS Code (File > Open Folder)

# 2. Create a virtual environment
python -m venv .venv

# 3. Activate it
#    Windows (PowerShell):
.venv\Scripts\Activate.ps1
#    Windows (cmd.exe):
.venv\Scripts\activate.bat
#    macOS/Linux:
source .venv/bin/activate

# 4. In VS Code: Ctrl+Shift+P -> "Python: Select Interpreter" -> pick .venv

# 5. Install dependencies
pip install -r requirements.txt

# 6. Create your .env file in the project root (same folder as README.md)
```

### 3.3 `.env` file

Create a file named exactly `.env` in the project root:

```
TELEGRAM_BOT_TOKEN=123456789:AAExampleTokenFromBotFather
HEARTBEAT_CHAT_ID=7279274530
ALPHA_VANTAGE_API_KEY=
PUBLIC_URL=
KEEPALIVE_INTERVAL_MINUTES=10
KEEPALIVE_TELEGRAM_NOTIFY=true
```

| Variable | Required? | What it's for |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **Yes** | From BotFather. Without this the bot won't start at all. |
| `HEARTBEAT_CHAT_ID` | Recommended | The chat ID that gets the hourly "still alive" heartbeat message. Send `/start` to your bot, check the log line `crypto-telegram-bot: /start from chat <id>` to find your chat ID, or leave blank to disable heartbeat messages. |
| `ALPHA_VANTAGE_API_KEY` | Optional | Only enables one extra news source. Leave blank to skip it — nothing else breaks. |
| `PUBLIC_URL` | Only matters when hosted (e.g. Render) | Your public URL, used by keepalive to self-ping. Leave blank when running locally — keepalive just logs that it's disabled and does nothing. |
| `KEEPALIVE_INTERVAL_MINUTES` | Optional (default `10`) | How often keepalive self-pings when `PUBLIC_URL` is set. Irrelevant locally. |
| `KEEPALIVE_TELEGRAM_NOTIFY` | Optional (default `true`) | Sends a Telegram message on every keepalive check. Set `false` to keep only the web dashboard updated, no extra messages. |
| `PORT` | Optional (default `10000`) | Local port for the status dashboard/health server. Render sets this automatically when deployed. |

### 3.4 Run it

In the VS Code terminal (with the venv active):

```bash
python -m bot.main
```

You should see log lines ending in `Bot starting - polling for updates...`.
Open Telegram, message your bot `/start`, and the main menu keyboard
should appear.

Optional: open `http://localhost:10000` in a browser while it's running
to see the status dashboard.

### 3.5 Running tests

```bash
pytest
```

---

## 4. Deploying to Render.com

The included `render.yaml` lets Render auto-configure everything if you
connect your GitHub repo via **Dashboard → New → Blueprint**. If you'd
rather set it up by hand, match these settings:

- **Type:** Web Service (not Background Worker — see note below)
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `python -m bot.main`
- **Environment variables:** same table as §3.3, entered in the Render
  dashboard instead of a `.env` file (Render injects them as real env
  vars — never commit your token to the repo).
- Also set `PYTHON_VERSION=3.12.0` so Render doesn't pick an untested
  version.

**Why "Web Service" and not "Background Worker"?** This bot only makes
*outbound* long-polling requests to Telegram — it doesn't need to
receive inbound HTTP traffic. But Render's **free tier only offers Web
Services** (Background Workers need a paid plan), so `bot/main.py`
starts a small status/health server (`web/dashboard.py`) on `$PORT`
purely to satisfy Render's "detected an open port" requirement.

### 4.1 Two things that determine whether it's really 24/7

1. **Render's free Web Services spin down after ~15 minutes with no
   inbound HTTP request**, and nothing calls the bot's own endpoint on
   its own — so a free instance *will* fall asleep and stop polling
   Telegram unless something pings it.
   - **Fix included:** set `PUBLIC_URL` to your Render URL
     (`https://<your-service-name>.onrender.com`) in the environment
     variables. `jobs/keepalive.py` will then self-ping every
     `KEEPALIVE_INTERVAL_MINUTES` (default 10, keep it ≤14 since Render
     sleeps at 15) to keep the instance awake.
   - Belt-and-suspenders (optional, free, no conflict): also point an
     external monitor like **UptimeRobot** or **cron-job.org** at the
     same URL every 5–10 minutes.
   - The only real fix that removes the sleep risk entirely is the paid
     **Starter** plan (`plan: starter` in `render.yaml`), which never
     spins down.

2. **`database/bot_state.db` lives on local disk**, which Render wipes
   on every deploy/restart *unless* you attach a persistent disk (needs
   Starter plan or higher — uncomment the `disk:` block in
   `render.yaml`). On the free plan, a restart just means everyone's
   toggled modes (24/7 Market Analyse On/Off etc.) reset to off — the
   bot itself won't crash or misbehave, people just need to re-press
   their buttons.

### 4.2 What you get once deployed

- Visit `https://<your-service>.onrender.com` any time → a small
  dashboard showing current time, last/next keepalive check,
  check/fail counts, and process uptime. Auto-refreshes every 30s.
- `https://<your-service>.onrender.com/status` → the same data as JSON.
- Hourly heartbeat + (optionally) every-10-min keepalive confirmation,
  both sent to `HEARTBEAT_CHAT_ID` on Telegram.

Render's free plan includes 750 instance-hours/month per workspace —
one service running continuously uses ~730–745 hours/month, so it fits
as long as it's the *only* always-on free service in that workspace.

---

## 5. Configuration (`config/settings.yaml`)

Every threshold and interval the bot uses lives here — not hardcoded —
so you can tune behaviour without touching code. Key sections:

- `signal_scoring` — buy/sell thresholds for the verdict engine
- `risk_management` / `money_management` — stop-loss %, take-profit %,
  risk per trade, min/max leverage
- `volume_spike_watch`, `absolute_volume_watch`, `daily_mover_watch`,
  `volatility_tier_watch` — what "24/7 Market Analyse" alerts on
- `strong_signal_watch` — scan interval, confidence threshold, worker
  count for "Find 24/7 Strong Signal"
- `search_signal` — worker count and top-N for the one-shot scan
- `heartbeat`, `signal_outcome_tracker`, `logging` — background job
  settings

Restart the bot after editing this file for changes to take effect.

---

## 6. Notes / known limitations

- Keepalive **prevents** sleep, it can't **undo** it — if the process
  is already down (crashed, mid-redeploy), it can't ping itself awake
  because it isn't running yet.
- Bitget's public market-data endpoints (used for all scanning) don't
  require an API key at all — only the optional news feature does.
- Logs are written per-run to `logs/<timestamp>.txt` (see
  `config/settings.yaml → logging`) and are git-ignored; they're the
  first place to check if something isn't behaving as expected.