"""Unit tests for the GEX entry/exit logic in strategy.py (STRATEGY='gex').

Pure — no IBKR. Feeds synthetic 1-min bars + synthetic Gflip/walls and checks the
three-condition entry (regime · breakout · momentum) and the OR-reclaim invalidation.

Run:  python scripts/test_gex_strategy.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import numpy as np
import pandas as pd

import config
import strategy

config.GEX_OR_MINUTES = 15
config.GEX_MOMENTUM_BARS = 2
config.GEX_WALL_TOL_PCT = 0.0015

_fails = []


def check(name, cond):
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}")
    if not cond:
        _fails.append(name)


def make_intraday(closes, start='2026-08-10 09:30'):
    idx = pd.date_range(start, periods=len(closes), freq='1min', tz='America/New_York')
    c = np.array(closes, float)
    return pd.DataFrame({'open': np.r_[c[0], c[:-1]], 'high': c + 0.1, 'low': c - 0.1,
                         'close': c, 'volume': 1}, index=idx)


# 15-min opening range → high 6010, low 5990 ; then a clean 3-bar breakout above
OR = [6000, 5990, 6010, 6000, 6005, 5995, 6008, 5992, 6010, 6000, 5990, 6005, 6000, 5998, 6002]
UP_BREAK = OR + [6004, 6006, 6008, 6012, 6015]     # last 3 closes accelerate up, 6015 > 6010(+.1)


def test_opening_range():
    print("\nopening_range_levels — 9:30-9:45 high/low")
    df = make_intraday(UP_BREAK)
    now = df.index[-1]
    hi, lo = strategy.opening_range_levels(df, now, 15)
    check("OR high ≈ 6010", abs(hi - 6010.1) < 0.2)
    check("OR low ≈ 5990", abs(lo - 5989.9) < 0.2)


def test_entry_all_conditions():
    print("\ngex_entry_signal — fires only when regime + breakout + momentum all hold")
    df = make_intraday(UP_BREAK)
    now = df.index[-1]
    zones = {'call_walls': [], 'put_walls': []}

    # negative-gamma regime (spot 6015 < Gflip 6050) + breakout + accel → CALL
    d, reason, ind, lean = strategy.gex_entry_signal('SPX', df, now, gflip=6050, zones=zones)
    check("neg-γ + breakout + accel → CALL", d == 'CALL')
    check("indicators carry Gflip + regime", ind.get('gflip') == 6050 and ind.get('regime') == 'negative')

    # regime fails: spot 6015 > Gflip 6000 (positive gamma) and no wall → no entry
    d2, *_ = strategy.gex_entry_signal('SPX', df, now, gflip=6000, zones=zones)
    check("positive-γ + no wall → blocked", d2 is None)

    # wall-breakout rescues the regime even in positive gamma
    zwall = {'call_walls': [(6010, 5000)], 'put_walls': []}
    d3, *_ = strategy.gex_entry_signal('SPX', df, now, gflip=6000, zones=zwall)
    check("positive-γ BUT breaking out of a call wall → CALL", d3 == 'CALL')

    # momentum fails: last 3 closes not monotonic up
    df_nm = make_intraday(OR + [6004, 6006, 6008, 6015, 6012])
    d4, *_ = strategy.gex_entry_signal('SPX', df_nm, df_nm.index[-1], gflip=6050, zones=zones)
    check("no acceleration → blocked", d4 is None)

    # breakout fails: price never clears the OR high
    df_nb = make_intraday(OR + [6002, 6004, 6006, 6008, 6009])
    d5, *_ = strategy.gex_entry_signal('SPX', df_nb, df_nb.index[-1], gflip=6050, zones=zones)
    check("no OR breakout → blocked", d5 is None)


def test_invalidation():
    print("\ngex_invalidated — 3 closes back inside the opening range")
    inside = make_intraday(OR + [6015, 6005, 6000, 6002])       # last 3 all inside [5990,6010]
    outside = make_intraday(OR + [6015, 6005, 6012, 6002])      # one close (6012) still outside
    check("3 closes back inside OR → invalidated", strategy.gex_invalidated(inside, 6010.1, 5989.9, 3) is True)
    check("a close still outside → not invalidated", strategy.gex_invalidated(outside, 6010.1, 5989.9, 3) is False)


if __name__ == '__main__':
    test_opening_range()
    test_entry_all_conditions()
    test_invalidation()
    print()
    if _fails:
        print(f"❌ {len(_fails)} FAILED: {_fails}")
        sys.exit(1)
    print("✅ all GEX-strategy tests passed")
