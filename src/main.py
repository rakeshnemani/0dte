import asyncio
# Python 3.10+ no longer auto-creates an event loop; ib_insync's eventkit
# dependency calls get_event_loop() at import time, so we must create one first.
asyncio.set_event_loop(asyncio.new_event_loop())

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
