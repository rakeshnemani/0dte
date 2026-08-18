"""Offline unit tests for the #30 close-integrity fixes (no IBKR needed).

    python scripts/test_close_integrity.py

Covers all seven failure scenarios around closing orders:
  1. Order reported Cancelled but FULLY FILLED  -> booked (the 2026-07-10 bug)
  2. Partial fill under Cancelled               -> slice booked, qty shrunk, remainder ACTIVE
  3. Requantify=0 with prior fills              -> booked instead of resubmitted
  4. Requantify=0, no fills anywhere            -> dropped as externally closed (no fabricated P&L)
  5. Inverse position (over-closed)             -> halted + alert, still tracked
  6. Position feed unknown                      -> close deferred (no blind submit)
  7. Partial remaining (3 of 6)                 -> close requantified to actual remainder
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import pandas as pd

import audit
import bot
import config
import market_time
import notifier

audit.record = lambda *a, **k: None       # never touch audit.csv from tests
notifier.send = lambda *a, **k: None      # silence Discord


def fresh_bot(pos):
    b = bot.TradingBot.__new__(bot.TradingBot)
    b.active_trades = {}
    b.closed_trades_today = []
    b.consecutive_losses = 0
    b.circuit_breaker_tripped = False
    b.daily_loss_limit_hit = False
    b.invalidation_counts = {}
    b.invalidation_total_today = 0

    class FakeBroker:
        def position_qty(self, cid): return pos
        def order_commission(self, t): return 2.0
        def order_perm_id(self, t): return 999
        def last_order_error(self, t): return (0, '')
        def place_limit(self, c, a, q, p):
            return NS(order=NS(orderId=77),
                      orderStatus=NS(status='Submitted', filled=0, avgFillPrice=0))
        def cancel_open_orders_for(self, r, except_order_id=None): return 0
        def option_tick(self, s, price=None): return 0.05
        def snap_to_tick(self, s, price):
            t = self.option_tick(s, price); return round(round(price / t) * t, 2)
        def option_symbol(self, s): return s
        def sleep(self, n): pass
        def fetch_intraday_data(self, s): return pd.DataFrame()
    b.broker = FakeBroker()
    return b


def trade(qty=6, status='ACTIVE'):
    return {'status': status, 'qty': qty, 'direction': 'PUT', 'structure': 'SINGLE',
            'entry_price': 0.30, 'option_contract': None, 'long_conid': 111,
            'leg_conids': [111], 'max_profit_pct': 0.0, 'exit_reason': 'test'}


def dead_order(filled, avg):
    return NS(order=NS(orderId=1),
              orderStatus=NS(status='Cancelled', filled=filled, avgFillPrice=avg),
              log=[], fills=[])


# 1. Cancelled but fully filled -> booked
b = fresh_bot(pos=0.0); t = trade(status='PENDING_EXIT')
t['exit_ibkr_trade'] = dead_order(6, 0.45)
t['exit_submitted_at'] = market_time.now_et()
b.active_trades = {'SPY': t}
b._check_pending_exit('SPY', t)
assert 'SPY' not in b.active_trades and len(b.closed_trades_today) == 1
print('1. Cancelled-but-filled -> BOOKED (the 07-10 double-fill bug is closed)')

# 2. Partial fill under Cancelled -> slice booked, qty shrunk, remainder ACTIVE
b = fresh_bot(pos=4.0); t = trade(status='PENDING_EXIT')
t['exit_ibkr_trade'] = dead_order(2, 0.44)
t['exit_submitted_at'] = market_time.now_et()
b.active_trades = {'SPY': t}
b._check_pending_exit('SPY', t)
assert t['qty'] == 4 and t['status'] == 'ACTIVE' and len(b.closed_trades_today) == 1
print('2. Partial fill -> slice booked, qty 6->4, remainder ACTIVE')

# 3. Requantify=0 with prior fills -> booked instead of resubmitted
b = fresh_bot(pos=0.0); t = trade()
t['exit_ibkr_trade'] = dead_order(6, 0.42)
b.active_trades = {'SPY': t}
b.close_position('SPY', 0.40, 'retry')
assert 'SPY' not in b.active_trades and len(b.closed_trades_today) == 1
print('3. Requantify=0 + prior fills -> BOOKED, no resubmit')

# 4. Requantify=0, no fills anywhere -> dropped as external, nothing fabricated
b = fresh_bot(pos=0.0); t = trade(); b.active_trades = {'SPY': t}
b.close_position('SPY', 0.40, 'x')
assert 'SPY' not in b.active_trades and len(b.closed_trades_today) == 0
print('4. Requantify=0, no fills -> dropped as externally closed')

# 5. Inverse position -> halted + still tracked
b = fresh_bot(pos=-6.0); t = trade(); b.active_trades = {'SPY': t}
b.close_position('SPY', 0.40, 'x')
assert t.get('close_failed') is True and 'SPY' in b.active_trades
print('5. Inverse (over-closed) -> HALTED + alert, still tracked')

# 6. Feed unknown -> defer, no submit, no attempt burned
b = fresh_bot(pos=None); t = trade(); b.active_trades = {'SPY': t}
b.close_position('SPY', 0.40, 'x')
assert t['status'] == 'ACTIVE' and t.get('close_attempts', 0) == 0
print('6. Feed unknown -> deferred (no blind submit)')

# 7. Partial remaining -> close requantified to the true remainder
b = fresh_bot(pos=3.0); t = trade(); b.active_trades = {'SPY': t}
b.close_position('SPY', 0.40, 'x')
assert t['qty'] == 3 and t['status'] == 'PENDING_EXIT'
print('7. Requantify 6->3 -> close submitted for actual remainder')

print('\nALL 7 #30 SCENARIOS PASS')
