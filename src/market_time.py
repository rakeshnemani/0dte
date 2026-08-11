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


def in_windows(windows: str) -> bool:
    """True if now (ET) is inside one of the comma-separated HH:MM-HH:MM windows."""
    now = now_et()
    for part in windows.split(','):
        try:
            a, b = part.strip().split('-')
            ah, am = (int(x) for x in a.split(':'))
            bh, bm = (int(x) for x in b.split(':'))
        except ValueError:
            continue
        start = now.replace(hour=ah, minute=am, second=0, microsecond=0)
        end = now.replace(hour=bh, minute=bm, second=0, microsecond=0)
        if start <= now < end:
            return True
    return False


def in_trend_window() -> bool:
    """True if now is inside a config.TREND_WINDOWS slot (STRATEGY=='trend')."""
    return in_windows(config.TREND_WINDOWS)


def in_gex_window() -> bool:
    """True if now is inside a config.GEX_WINDOWS slot (STRATEGY=='gex')."""
    return in_windows(config.GEX_WINDOWS)


def past_gex_flatten() -> bool:
    """True once we've reached config.GEX_FLATTEN_TIME (e.g. 15:50) — GEX flattens then."""
    now = now_et()
    try:
        h, m = (int(x) for x in config.GEX_FLATTEN_TIME.split(':'))
    except ValueError:
        h, m = 15, 50
    return now >= now.replace(hour=h, minute=m, second=0, microsecond=0)


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
