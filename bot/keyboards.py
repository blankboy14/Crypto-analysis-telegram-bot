"""
bot/keyboards.py

Phase 1.2 - the main vertical menu keyboard shown to every user (via
/start, in bot/handlers/start.py). "Vertical" means one button per
row, per the spec - not a 2-per-row grid.

Label text constants are exported here so every handler that needs to
recognize which button was pressed imports the constant instead of
retyping the string. One typo in a duplicated literal is a silent bug
(the button press would never match any handler) - this file is the
single source of truth for the exact wording.

NOTE: this is the *main* menu only (the 5 buttons from Phase 1.2). The
"Spot / Future / Both" inline keyboard that appears after picking any
of the three mode buttons is common to all three and belongs in
bot/handlers/market_select.py per the file plan, not here.
"""

from telegram import ReplyKeyboardMarkup

# --- Phase 1.2 button labels ---
BTN_MARKET_ANALYSE_ON = "📊 24/7 Market Analyse"
BTN_MARKET_ANALYSE_OFF = "🔕 24/7 Off Market Analyse"
BTN_STRONG_SIGNAL_ON = "🔥 Find 24/7 Strong Signal"
BTN_STRONG_SIGNAL_OFF = "🛑 Off 24/7 Find Signal"
BTN_SEARCH_SIGNAL = "🔎 Search Signal"

# Order here = order shown on the keyboard (top to bottom).
MAIN_MENU_BUTTONS = [
    BTN_MARKET_ANALYSE_ON,
    BTN_MARKET_ANALYSE_OFF,
    BTN_STRONG_SIGNAL_ON,
    BTN_STRONG_SIGNAL_OFF,
    BTN_SEARCH_SIGNAL,
]


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """
    Builds the Phase 1.2 vertical menu: exactly one button per row.

    resize_keyboard=True keeps it compact on mobile instead of taking
    over half the screen; is_persistent=True keeps it visible after a
    tap instead of Telegram's default one-shot-then-hide behaviour,
    since these are 24/7 toggles the user will come back to repeatedly.
    """
    rows = [[label] for label in MAIN_MENU_BUTTONS]
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose an option...",
    )