import os
from dotenv import load_dotenv

load_dotenv()

# IBKR connection settings
IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "4002"))        # 4002 = IB Gateway paper, 7497 = TWS paper
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))

SYMBOLS = os.getenv("SYMBOLS", "SPY,QQQ,IWM").split(",")
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "200.0"))
# Each (symbol, direction) pair cools down for SIGNAL_COOLDOWN_MINUTES after a trade,
# then can re-trigger. MAX_TRADES_PER_DAY is the overall daily cap across all symbols.
# With 3 symbols × 2 directions and a 30-min cooldown in a 5.5-hr window: ~12 is realistic.
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "12"))
SIGNAL_COOLDOWN_MINUTES = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "30"))
MIN_SPREAD_COST = float(os.getenv("MIN_SPREAD_COST", "0.10"))
STRIKE_STEP = {"SPY": 1, "QQQ": 1, "IWM": 1, "XSP": 1, "SPX": 25}
SPREAD_WIDTH = {"SPY": 1, "QQQ": 1, "IWM": 1, "XSP": 1, "SPX": 5}

TAKE_PROFIT_TRAIL_TRIGGER = float(os.getenv("TAKE_PROFIT_TRAIL_TRIGGER", "0.40"))
TRAILING_STOP_LOSS_PCT = float(os.getenv("TRAILING_STOP_LOSS_PCT", "0.10"))
MAX_PROFIT_EXIT_MULTIPLIER = float(os.getenv("MAX_PROFIT_EXIT_MULTIPLIER", "0.70"))
HARD_STOP_LOSS_PCT = float(os.getenv("HARD_STOP_LOSS_PCT", "0.70"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5"))
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
