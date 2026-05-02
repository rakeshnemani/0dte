import os
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

SYMBOLS = os.getenv("SYMBOLS", "SPY,SPX").split(",")
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "200.0"))

HARD_STOP_LOSS_PCT = float(os.getenv("HARD_STOP_LOSS_PCT", "-0.50"))
TAKE_PROFIT_TRAIL_TRIGGER = float(os.getenv("TAKE_PROFIT_TRAIL_TRIGGER", "0.40"))
TRAILING_STOP_LOSS_PCT = float(os.getenv("TRAILING_STOP_LOSS_PCT", "0.10"))
MAX_PROFIT_EXIT_MULTIPLIER = float(os.getenv("MAX_PROFIT_EXIT_MULTIPLIER", "0.70"))
