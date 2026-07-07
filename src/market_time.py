"""Market-hours helpers. All times America/New_York (ET)."""
import datetime

import pytz

import config

TZ = pytz.timezone('America/New_York')


def now_et() -> datetime.datetime:
    return datetime.datetime.now(TZ)


def market_open_today() -> datetime.datetime:
    return now_et().replace(hour=9, minute=30, second=0, microsecond=0)


def is_market_open() -> bool:
    now = now_et()
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close


def is_entry_window() -> bool:
    """No new entries after 3:00 PM ET."""
    return now_et().hour < 15


def is_eod_flatten_time() -> bool:
    """True once we've reached the end-of-day flatten time on a trading day."""
    now = now_et()
    flatten_at = now.replace(hour=config.EOD_FLATTEN_HOUR,
                             minute=config.EOD_FLATTEN_MINUTE,
                             second=0, microsecond=0)
    return now >= flatten_at


def seconds_until_market_open() -> int:
    """Seconds until the next 9:30 AM ET weekday open."""
    now = now_et()
    next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now >= next_open:
        next_open += datetime.timedelta(days=1)
    while next_open.weekday() >= 5:   # skip Saturday (5) and Sunday (6)
        next_open += datetime.timedelta(days=1)
    return max(int((next_open - now).total_seconds()), 60)
