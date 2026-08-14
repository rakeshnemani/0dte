"""Regression tests for the 2026-08-13 single-leg execution bugs.

That day the FIRST real GEX signal (BUY SPXW 7800C) failed to execute for two
independent reasons, both fixed here:
  A. limit 11.65 was rejected by IBKR error 110 — SPX options tick $0.10 at
     premium ≥ $3, not $0.05, so 11.65 is not a valid increment.
  B. notify_submit / notify_filled crashed formatting the single-leg indicator
     dict (adx/vwap/orb are explicit None) and dereferencing trade['short_strike']
     (single-leg trades have no short leg).
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())   # eventkit/ib_insync need a loop at import

import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from broker import IBKRBroker
import notifier


def test_option_tick_price_aware():
    tick = IBKRBroker.option_tick.__get__(object.__new__(IBKRBroker))
    # SPX: $0.10 at/above $3.00, $0.05 below, $0.10 when price unknown
    assert tick('SPX', 11.65) == 0.10, "≥$3 SPX premium must tick $0.10 (else IBKR err 110)"
    assert tick('SPX', 3.00) == 0.10
    assert tick('SPX', 2.95) == 0.05
    assert tick('SPX', None) == 0.10, "unknown price must default to the always-valid $0.10"
    # Equity/ETF options tick a penny
    assert tick('SPY', 11.65) == 0.01
    print("✓ option_tick price-aware ($0.10 ≥ $3, $0.05 <$3, penny for equities)")


def test_limit_snaps_to_valid_tick():
    """Replay the 10:01 pricing: mid 11.65 / ask 11.70 must snap to a 0.10 grid."""
    mid, ask, aggression = 11.65, 11.70, 0.5
    raw = mid + aggression * max(ask - mid, 0.0)
    tick = IBKRBroker.option_tick.__get__(object.__new__(IBKRBroker))('SPX', raw)
    snap = lambda p: round(round(p / tick) * tick, 2)
    limit = max(snap(raw), snap(mid))
    assert tick == 0.10
    assert round(limit * 100) % 10 == 0, f"limit {limit} is not a valid $0.10 increment"
    assert limit >= snap(mid), "limit must not fall below the (tick-aligned) mid"
    print(f"✓ limit snaps to a valid $0.10 tick (raw {raw} → {limit}); no more error 110")


def _single_leg_indicators():
    # exactly what strategy.py emits for single-leg trend/gex (adx/vwap/orb = None)
    return {'current_price': 7802.26, 'supertrend_dir': 1, 'psar_dir': 1, 'kauf': 12.0,
            'adx': None, 'vwap': None, 'orb_high': None, 'orb_low': None}


def test_notify_submit_no_crash_single_leg():
    sent = []
    notifier.send = lambda title, desc, color: sent.append(desc)
    # single-leg passes the strike twice (long == short)
    notifier.notify_submit('SPX', 'CALL', 7800, 7800, 11.60, 1, 5000,
                           _single_leg_indicators(), 'GEX wall-breakout @7800', 43315)
    assert len(sent) == 1
    assert 'Single-leg' in sent[0] and 'ADX: 0.00' in sent[0]
    print("✓ notify_submit renders single-leg without crashing on None indicators")


def test_notify_filled_no_crash_single_leg():
    sent = []
    notifier.send = lambda title, desc, color: sent.append(desc)
    trade = {'structure': 'SINGLE', 'direction': 'CALL', 'long_strike': 7800.0, 'qty': 1,
             'entry_indicators': _single_leg_indicators(), 'reason': 'GEX wall-breakout @7800'}
    notifier.notify_filled('SPX', trade, 11.60)   # no 'short_strike'/'tp_price' → must not KeyError
    assert len(sent) == 1
    assert 'Single-leg' in sent[0]
    print("✓ notify_filled handles single-leg (no short_strike KeyError, no None crash)")


if __name__ == '__main__':
    test_option_tick_price_aware()
    test_limit_snaps_to_valid_tick()
    test_notify_submit_no_crash_single_leg()
    test_notify_filled_no_crash_single_leg()
    print("\nAll single-leg execution regression tests passed ✓")
