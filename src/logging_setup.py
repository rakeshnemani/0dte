"""Operational logging — console + a daily-rotating file at logs/bot.log.

Distinct from audit.csv (which is *financials only*). This is the *operational*
record: every order submitted, IBKR error, reconnect, exit decision, reconciliation
drop, dashboard rebuild — kept on disk so a past run can be debugged after the
terminal scrollback is long gone. Timestamps are ET to line up with audit.csv.

Call configure() once, before importing anything that logs (i.e. first thing in
main.py). One file per day, 30-day retention.
"""
import datetime
import logging
import os
from logging.handlers import TimedRotatingFileHandler

import pytz

ET = pytz.timezone('America/New_York')
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
LOG_FILE = os.path.join(LOG_DIR, 'bot.log')


class _ETFormatter(logging.Formatter):
    """Formatter that stamps records in America/New_York, not machine-local time."""
    def formatTime(self, record, datefmt=None):
        dt = datetime.datetime.fromtimestamp(record.created, ET)
        return dt.strftime(datefmt or '%Y-%m-%d %H:%M:%S %Z')


def configure(level=logging.INFO):
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = _ETFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    root = logging.getLogger()
    root.setLevel(level)
    # Own the config outright — drop any handler a stray basicConfig may have added
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # One file per day: bot.log, then bot.log.2026-07-09 etc. Kept 30 days.
    fileh = TimedRotatingFileHandler(LOG_FILE, when='midnight', backupCount=30)
    fileh.setFormatter(fmt)
    fileh.suffix = '%Y-%m-%d'
    root.addHandler(fileh)

    logging.getLogger(__name__).info(
        f"Operational logging → console + {LOG_FILE} (daily rotation, 30-day retention)"
    )
