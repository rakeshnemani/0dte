"""Unit tests for the trend strategy (STRATEGY='trend') — pure functions in strategy.py.

Pins the Supertrend/PSAR/Kaufman indicators and the windowed entry/exit signals that
the live bot uses in trend mode, so they keep matching scripts/backtest_spread_dollars.py.
No IBKR connection — everything runs on synthetic bars.

Run:  python scripts/test_trend_strategy.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import numpy as np
import pandas as pd

import config
import market_time
import strategy

# Deterministic params (don't depend on .env)
config.TREND_SUPERTREND_PERIOD = 7
config.TREND_SUPERTREND_MULT = 3.0
config.TREND_KAUF_N = 14
config.TREND_KAUF_MAX = 50
config.TREND_MIN_BARS = 20
config.TREND_WINDOWS = "09:30-10:00,15:00-16:00"

_fails = []


def check(name, cond):
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}")
    if not cond:
        _fails.append(name)


def make_df(closes, start='2026-08-10 09:30'):
    idx = pd.date_range(start, periods=len(closes), freq='1min', tz='America/New_York')
    c = np.array(closes, float)
    return pd.DataFrame({'open': np.r_[c[0], c[:-1]], 'high': c + 0.1, 'low': c - 0.1,
                         'close': c, 'volume': 1}, index=idx)


def flip_slice(closes, want):
    """Build df from closes, find the first Supertrend flip to `want` (+1/−1),
    return (df sliced to end ON that flip bar, flip index)."""
    df = make_df(closes)
    sdir = strategy.supertrend_dir(df, 7, 3.0)
    for i in range(config.TREND_MIN_BARS, len(sdir)):    # skip warmup flips (unstable ATR)
        if sdir[i] == want and sdir[i - 1] != want and sdir[i - 1] != 0:
            return df.iloc[:i + 1], i
    return None, None


# ── indicators ────────────────────────────────────────────────────────────────
def test_indicators():
    print("\nindicators — Supertrend direction / Kaufman chop")
    up = strategy.supertrend_dir(make_df([100 + 0.7 * i for i in range(40)]), 7, 3.0)
    dn = strategy.supertrend_dir(make_df([140 - 0.7 * i for i in range(40)]), 7, 3.0)
    check("Supertrend = +1 on a clean uptrend", up[-1] == 1)
    check("Supertrend = -1 on a clean downtrend", dn[-1] == -1)
    check("Kaufman ~0 on a straight-line trend",
          strategy.kaufman_chop(make_df([100 + i for i in range(30)]), 14)[-1] < 10)
    check("Kaufman high (>60) on a 1-tick chop",
          strategy.kaufman_chop(make_df([100 + (i % 2) for i in range(30)]), 14)[-1] > 60)


# ── entry signal ────────────────────────────────────────────────────────────────
def test_entry_signal():
    print("\ntrend_entry_signal — fires only on a fresh flip that clears the gates")
    now = market_time.now_et()

    # a sharp down-flip: Supertrend 1→-1, PSAR agrees (-1), kauf ~58.8
    down = [100 + 0.5 * i for i in range(30)] + [115 - 1.5 * i for i in range(1, 22)]
    dfx, fi = flip_slice(down, -1)
    check("built a down-flip fixture", dfx is not None)

    config.TREND_KAUF_MAX = 100                       # neutralize kauf gate → test direction
    d, reason, ind, lean = strategy.trend_entry_signal('SPX', dfx, now)
    check("down-flip → PUT (Supertrend flip + PSAR agree)", d == 'PUT')
    check("indicators carry the kauf value", 'kauf' in ind)

    config.TREND_KAUF_MAX = 50                        # the real gate: kauf 58.8 > 50 → blocked
    d2, _, _, _ = strategy.trend_entry_signal('SPX', dfx, now)
    check("same flip blocked when kauf (58.8) > TREND_KAUF_MAX(50)", d2 is None)

    # an up-flip → CALL
    up = [130 - 0.5 * i for i in range(30)] + [115 + 1.5 * i for i in range(1, 22)]
    dfu, _ = flip_slice(up, 1)
    config.TREND_KAUF_MAX = 100
    du, _, _, _ = strategy.trend_entry_signal('SPX', dfu, now)
    check("up-flip → CALL", du == 'CALL')
    config.TREND_KAUF_MAX = 50

    # mid-trend (no flip on the last bar) → no signal
    steady = make_df([100 - 0.7 * i for i in range(40)])
    dm, _, _, _ = strategy.trend_entry_signal('SPX', steady, now)
    check("persisting trend (no flip) → no entry", dm is None)

    # too few bars → no signal, no crash
    dshort, _, _, _ = strategy.trend_entry_signal('SPX', make_df([100 + i for i in range(10)]), now)
    check("insufficient bars → no entry (no crash)", dshort is None)


# ── reversal exit ────────────────────────────────────────────────────────────────
def test_trend_reversed():
    print("\ntrend_reversed — Supertrend turned against the position")
    down = make_df([140 - 0.7 * i for i in range(40)])   # Supertrend now -1
    check("CALL exits when Supertrend is -1", strategy.trend_reversed('CALL', down) is True)
    check("PUT does NOT exit when Supertrend is -1", strategy.trend_reversed('PUT', down) is False)
    up = make_df([100 + 0.7 * i for i in range(40)])     # Supertrend now +1
    check("PUT exits when Supertrend is +1", strategy.trend_reversed('PUT', up) is True)
    check("CALL does NOT exit when Supertrend is +1", strategy.trend_reversed('CALL', up) is False)


# ── time windows ────────────────────────────────────────────────────────────────
def test_windows():
    print("\nin_trend_window — only 9:30-10:00 and 15:00-16:00 ET")
    tz = market_time.TZ
    saved = market_time.now_et
    try:
        for hh, mm, want in [(9, 45, True), (10, 30, False), (12, 0, False),
                             (15, 30, True), (15, 59, True), (16, 0, False)]:
            market_time.now_et = (lambda h=hh, m=mm:
                                  tz.localize(datetime.datetime(2026, 8, 10, h, m)))
            got = market_time.in_trend_window()
            check(f"{hh:02d}:{mm:02d} → {'in' if want else 'out'}", got is want)
    finally:
        market_time.now_et = saved


if __name__ == '__main__':
    test_indicators()
    test_entry_signal()
    test_trend_reversed()
    test_windows()
    print()
    if _fails:
        print(f"❌ {len(_fails)} FAILED: {_fails}")
        sys.exit(1)
    print("✅ all trend-strategy tests passed")
