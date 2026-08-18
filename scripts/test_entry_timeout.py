"""Unit tests for #34 — entry-order timeout (stale-signal cancellation).

The 2026-07-15 loss: a limit BUY rested 1h42m, filled, and invalidated 65s later
(−$175). A resting limit only fills once the spread decays to our bid — i.e. once
the market has moved against the thesis. These tests pin the fix, and especially
the partial-fill path: a partly-filled order is a REAL position and must never be
dropped (#21/#30).

No IBKR connection — the broker and IBKR trade objects are faked.
Run:  python scripts/test_entry_timeout.py
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import config
import market_time

# CRITICAL: stub all real-world side effects. _activate_entry() writes to the live
# audit.csv and posts to Discord — a test must never touch either (it polluted the
# ledger on the first run). Neutralise them before importing bot.
import audit
import notifier
audit.record = lambda *a, **k: None
for _fn in ('notify_filled', 'notify_condor_filled', 'notify_entry_expired'):
    setattr(notifier, _fn, lambda *a, **k: None)

_fails = []


def check(name, cond):
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}")
    if not cond:
        _fails.append(name)


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeStatus:
    def __init__(self, status='Submitted', filled=0, avg=0.0):
        self.status, self.filled, self.avgFillPrice = status, filled, avg
        self.permId = 1


class FakeIbkrTrade:
    def __init__(self, status='Submitted', filled=0, avg=0.0):
        self.orderStatus = FakeStatus(status, filled, avg)
        self.order = object()
        self._done = status in ('Filled', 'Cancelled', 'Inactive')
        self.cancelled = False

    def isDone(self):
        return self._done


class FakeBroker:
    def __init__(self, on_cancel=None):
        self.on_cancel = on_cancel
        self.cancel_calls = 0

    def sleep(self, s):
        pass

    def cancel_order(self, order):
        self.cancel_calls += 1
        if self.on_cancel:
            self.on_cancel()

    def order_perm_id(self, t):
        return 42

    def order_commission(self, t):
        return 1.0

    def place_limit(self, *a, **k):
        return FakeIbkrTrade('Submitted')


def _make_bot():
    import bot as bot_mod
    b = bot_mod.TradingBot.__new__(bot_mod.TradingBot)   # skip __init__/IBKR
    b.active_trades = {}
    b.signal_cooldowns = {}       # cooldown/count now set on FILL (08-07)
    b.daily_trade_count = 0
    return b


def _trade(ibkr_trade, age_s, qty=21):
    return {
        'strategy': 'trend', 'symbol': 'XSP',
        'direction': 'PUT', 'status': 'PENDING_ENTRY', 'qty': qty,
        'target_entry_price': 0.14,
        'submitted_at': market_time.now_et() - datetime.timedelta(seconds=age_s),
        'ibkr_trade': ibkr_trade, 'bag_contract': object(),
        'entry_indicators': {'adx': 30.0}, 'reason': 'test', 'max_profit_pct': 0.0,
    }


# ── tests ────────────────────────────────────────────────────────────────────
def test_not_yet_expired():
    print("\n#34 — a young unfilled order is left alone")
    b = _make_bot(); b.broker = FakeBroker()
    t = FakeIbkrTrade('Submitted')
    tr = _trade(t, age_s=30)                      # 30s < 120s timeout
    b.active_trades['XSP'] = tr
    expired = b._expire_stale_entry('XSP', tr, t)
    check("30s old: not expired", expired is False)
    check("30s old: no cancel sent", b.broker.cancel_calls == 0)
    check("30s old: still tracked", 'XSP' in b.active_trades)


def test_expired_unfilled():
    print("\n#34 — a stale unfilled order is cancelled and dropped (the 07-15 case)")
    b = _make_bot(); b.broker = FakeBroker()
    t = FakeIbkrTrade('Submitted')
    tr = _trade(t, age_s=6153)                    # the real 1h42m wait
    b.active_trades['XSP'] = tr
    expired = b._expire_stale_entry('XSP', tr, t)
    check("1h42m old: expired", expired is True)
    check("1h42m old: cancel sent", b.broker.cancel_calls == 1)
    check("1h42m old: dropped from tracking (no position opened)",
          'XSP' not in b.active_trades)
    check("timed-out entry: NO cooldown set (fix #2 — signal free to re-fire)",
          b.signal_cooldowns == {})
    check("timed-out entry: NO daily-count burned", b.daily_trade_count == 0)


def test_expired_partial_fill_is_rescued():
    print("\n#34 — a PARTIALLY filled stale order is kept, never orphaned (#21/#30)")
    t = FakeIbkrTrade('Submitted')

    # The cancel races a partial fill: 8 of 21 lots fill just as we cancel.
    def on_cancel():
        t.orderStatus.status = 'Cancelled'
        t.orderStatus.filled = 8
        t.orderStatus.avgFillPrice = 0.14

    b = _make_bot(); b.broker = FakeBroker(on_cancel=on_cancel)
    tr = _trade(t, age_s=300, qty=21)
    b.active_trades['XSP'] = tr
    expired = b._expire_stale_entry('XSP', tr, t)
    check("partial fill: expired-path taken", expired is True)
    check("partial fill: position STILL TRACKED (not orphaned)", 'XSP' in b.active_trades)
    check("partial fill: promoted to ACTIVE", tr['status'] == 'ACTIVE')
    check("partial fill: qty requantified 21 → 8 (the real slice)", tr['qty'] == 8)
    check("partial fill: entry price booked from the fill", tr['entry_price'] == 0.14)
    check("FILL sets per-strategy cooldown ('trend','XSP','PUT')", ('trend', 'XSP', 'PUT') in b.signal_cooldowns)
    check("FILL burns one daily slot", b.daily_trade_count == 1)


def test_timeout_disabled():
    print("\n#34 — timeout=0 preserves the old wait-forever behavior")
    saved = config.ENTRY_ORDER_TIMEOUT_SECONDS
    config.ENTRY_ORDER_TIMEOUT_SECONDS = 0
    try:
        b = _make_bot(); b.broker = FakeBroker()
        t = FakeIbkrTrade('Submitted')
        tr = _trade(t, age_s=99999)
        b.active_trades['XSP'] = tr
        check("timeout=0: never expires", b._expire_stale_entry('XSP', tr, t) is False)
        check("timeout=0: no cancel sent", b.broker.cancel_calls == 0)
    finally:
        config.ENTRY_ORDER_TIMEOUT_SECONDS = saved


def test_cancelled_with_fill_not_dropped():
    print("\n#34/#30 — a Cancelled entry that actually FILLED is not dropped")
    b = _make_bot(); b.broker = FakeBroker()
    t = FakeIbkrTrade('Cancelled', filled=6, avg=0.31)
    tr = _trade(t, age_s=10, qty=6)
    b.active_trades['XSP'] = tr
    b._check_pending_fill('XSP', tr)
    check("cancelled-but-filled: still tracked", 'XSP' in b.active_trades)
    check("cancelled-but-filled: promoted to ACTIVE", tr['status'] == 'ACTIVE')

    # ...and a truly cancelled, zero-fill order IS dropped.
    b2 = _make_bot(); b2.broker = FakeBroker()
    t2 = FakeIbkrTrade('Cancelled', filled=0)
    tr2 = _trade(t2, age_s=10)
    b2.active_trades['XSP'] = tr2
    b2._check_pending_fill('XSP', tr2)
    check("cancelled, zero fill: correctly dropped", 'XSP' not in b2.active_trades)


if __name__ == '__main__':
    print(f"config: ENTRY_ORDER_TIMEOUT_SECONDS={config.ENTRY_ORDER_TIMEOUT_SECONDS}s "
          f"MIN_OPTION_COST=${config.MIN_OPTION_COST}")
    test_not_yet_expired()
    test_expired_unfilled()
    test_expired_partial_fill_is_rescued()
    test_timeout_disabled()
    test_cancelled_with_fill_not_dropped()
    print()
    if _fails:
        print(f"❌ {len(_fails)} FAILED: {_fails}")
        sys.exit(1)
    print("✅ all #34 entry-timeout tests passed")
