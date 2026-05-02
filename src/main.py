import time
from bot import TradingBot
import logging

logger = logging.getLogger(__name__)

def main():
    try:
        bot = TradingBot()
        bot.run()
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")

if __name__ == "__main__":
    main()
