"""Unit tests for single-leg directional trend (TREND_LEGS=single).

Pins the order-construction path (_place_single_leg), single-leg valuation, no-TP,
and the entry-time low-vol gate. No IBKR — the broker + IBKR objects are faked.

Run:  python scripts/test_single_leg.py
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import numpy as np
import pandas as pd

import audit
import notifier
audit.record = lambda *a, **k: None
for _fn in ('notify_submit', 'notify_today_summary', 'notify_filled'):
    setattr(notifier, _fn, lambda *a, **k: None)

import config
import strategy

_fails = []


def check(name, cond):
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}")
    if not cond:
        _fails.append(name)


class FakeContract:
    def __init__(self, conid): self.conId = conid


class FakeOrder:
    orderId = 55


class FakeIbkrTrade:
    def __init__(self): self.order = FakeOrder()


class FakeBroker:
    def __init__(self, bid=9.0, mid=9.5, ask=10.0, mid_now=9.5):
        self.q = (bid, mid, ask); self.mid_now = mid_now; self.placed = []
    def get_option_contract(self, symbol, direction, strike): return FakeContract(12345)
    def _get_option_quote(self, opt): return self.q
    def _get_option_mid(self, opt): return self.mid_now
    def option_tick(self, symbol, price=None): return 0.05
    def place_limit(self, contract, action, qty, price):
        self.placed.append((contract, action, qty, price)); return FakeIbkrTrade()


def _make_bot():
    import bot as bot_mod
    b = bot_mod.TradingBot.__new__(bot_mod.TradingBot)
    b.active_trades = {}
    b.closed_trades_today = []
    return b


def test_single_leg_order():
    print("\n_place_single_leg — buys ONE ATM option, no bag, no short leg")
    b = _make_bot(); b.broker = FakeBroker(bid=9.0, mid=9.5, ask=10.0)
    b._place_single_leg('SPX', 'CALL', 6000, {'current_price': 6000.0}, 'test up-flip', 'trend')
    tr = b.active_trades.get('trend:SPX')       # composite (strategy:symbol) key
    check("trade tracked under composite key 'trend:SPX'", tr is not None)
    check("trade carries strategy + symbol", tr and tr['strategy'] == 'trend' and tr['symbol'] == 'SPX')
    check("structure = SINGLE", tr and tr['structure'] == 'SINGLE')
    check("qty = 1", tr and tr['qty'] == 1)
    check("has option_contract, NO bag_contract",
          tr and 'option_contract' in tr and 'bag_contract' not in tr)
    check("single leg_conid tracked", tr and tr['leg_conids'] == [12345] and tr['long_conid'] == 12345)
    check("BUY order placed for 1 contract", b.broker.placed and b.broker.placed[0][1:3] == ('BUY', 1))
    # limit = mid + 0.5*(ask-mid) = 9.5 + 0.25 = 9.75, snapped to $0.05 tick
    check("limit priced mid→ask via ENTRY_AGGRESSION (9.75)", abs(b.broker.placed[0][3] - 9.75) < 1e-6)

    print("\n_current_value — single leg reads the option mid")
    b.broker.mid_now = 12.3
    check("current value = option mid", abs(b._current_value('SPX', tr) - 12.3) < 1e-6)


def test_lowvol_gate():
    print("\nentry_realized_vol — quiet day skipped, active day traded (gate 0.082)")
    quiet = pd.DataFrame({'close': [6000 + 0.1 * (i % 2) for i in range(30)]})
    active = pd.DataFrame({'close': [6000 + 10 * (i % 2) for i in range(30)]})
    vq = strategy.entry_realized_vol(quiet)
    va = strategy.entry_realized_vol(active)
    check(f"quiet-day vol {vq:.3f} < 0.082 (would skip)", vq < 0.082)
    check(f"active-day vol {va:.3f} > 0.082 (would trade)", va > 0.082)
    check("too-few bars → 0.0 (no crash)", strategy.entry_realized_vol(pd.DataFrame({'close': [1, 2]})) == 0.0)


if __name__ == '__main__':
    test_single_leg_order()
    test_lowvol_gate()
    print()
    if _fails:
        print(f"❌ {len(_fails)} FAILED: {_fails}")
        sys.exit(1)
    print("✅ all single-leg tests passed")
