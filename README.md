# crypto-telegram-bot

Telegram bot version of the crypto-analyzer web app - same analysis
engine (indicators, trading concepts, signal scanner, Bitget adapter),
delivered through a Telegram bot instead of a browser dashboard.

## Build status

This project is being built incrementally. What exists right now:

| File | Status |
|---|---|
| `.env` / `.env.example` | ✅ done |
| `.gitignore` | ✅ done |
| `requirements.txt` | ✅ done |
| `config/settings.yaml` | ✅ done |
| `engine/news_service.py` | ✅ done (AI/Claude classification removed - see file header) |
| everything else in the file plan (`bot/`, `jobs/`, `database/`, rest of `engine/`, `tests/`) | ⏳ not yet - carried over from the old web project or still to be written |

Don't try to run `bot/main.py` yet - it doesn't exist in this drop.

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

Not available yet in this drop - `bot/main.py` (the actual bot
entrypoint) hasn't been built. Once it exists, running it will look
like:

```
python -m bot.main
```

## Project layout

See the file plan this project follows for the full target structure
(`bot/`, `engine/`, `jobs/`, `database/`, `tests/`). `engine/` carries
over the existing analysis code (indicators, trading concepts, signal
scanner, Bitget adapter, order flow, news) from the web dashboard
project largely as-is; `bot/`, `jobs/`, and `database/` are
Telegram-bot-specific and built fresh.

## Notes

- `database/bot_state.db` (per-user toggle/mode state) and `logs/` are
  git-ignored - they're runtime data, not source.
- `database/indicator_toggles.json` IS committed - it's the shipped
  default indicator on/off state, not a runtime file.