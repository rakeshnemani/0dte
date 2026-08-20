"""Unit tests for the Thesis-GEX command rail (TODO #44).

Two layers:
  1. PURE — src/commands.py validate / trigger-evaluation / expiry (no IBKR, no bot).
  2. INTEGRATION — bot._process_thesis_commands + _watch_thesis_triggers drive a fake broker:
     an arm fires only when its trigger is met, close_if closes an ACTIVE position, cancel drops
     a pending arm, an expired arm is swept, a malformed file is rejected — and every processed
     file is moved out of the scan dir so it never runs twice.

No IBKR / Gateway — broker, market data and positions are faked. Run:
    python scripts/test_thesis_commands.py
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import datetime
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import pandas as pd

import audit
import commands
import notifier

audit.record = lambda *a, **k: None
for _fn in ('notify_submit', 'notify_today_summary', 'notify_filled',
            'notify_thesis_action', 'notify_closed', 'notify_closed_externally'):
    setattr(notifier, _fn, lambda *a, **k: None)

import market_time

_fails = []


def check(name, cond):
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}")
    if not cond:
        _fails.append(name)


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeContract:
    def __init__(self, conid, sec='OPT', symbol='SPXW'):
        self.conId = conid; self.secType = sec; self.symbol = symbol
        self.right = 'C'; self.strike = conid


class FakePos:
    def __init__(self, conid, pos=1):
        self.contract = FakeContract(conid); self.position = pos


class FakeIbkrTrade:
    class _O: orderId = 7
    class _S: filled = 0; avgFillPrice = 0.0
    def __init__(self): self.order = self._O(); self.orderStatus = self._S()


class FakeBroker:
    def __init__(self, spot=7700.0):
        self.set_spot(spot)
        self.positions_list = []
        self.placed = []

    def set_spot(self, spot, tail=None):
        closes = tail if tail is not None else [spot - 2, spot - 1, spot]
        self._df = pd.DataFrame({'close': closes})
        self._spot = spot

    def fetch_intraday_data(self, symbol): return self._df
    def get_current_price(self, symbol): return self._spot
    def get_option_contract(self, symbol, direction, strike): return FakeContract(int(strike))
    def _get_option_quote(self, opt): return (9.0, 9.5, 10.0)
    def _get_option_mid(self, opt): return 9.5
    def option_tick(self, symbol, price=None): return 0.05
    def snap_to_tick(self, symbol, price): return round(round(price / 0.05) * 0.05, 2)
    def option_symbol(self, symbol): return 'SPXW'
    def place_limit(self, *a, **k):
        self.placed.append((a, k)); return FakeIbkrTrade()
    def positions(self): return self.positions_list
    def position_qty(self, conid): return 1
    def cancel_open_orders_for(self, sym): return 0
    def fetch_gex_chain(self, *a, **k): return (self._spot, [])   # empty chain → context-less, no gex math


def _make_bot(tmp, spot=7700.0):
    import bot as bot_mod
    b = bot_mod.TradingBot.__new__(bot_mod.TradingBot)
    b.broker = FakeBroker(spot)
    b.active_trades = {}
    b.closed_trades_today = []
    b.daily_trade_count = 0
    b.signal_cooldowns = {}
    b.consecutive_losses = 0
    b.circuit_breaker_tripped = False
    b.daily_loss_limit_hit = False
    b._untracked_alerted = set()
    b._blocked_alert_at = {}
    b._gex_chain = []
    b._gex_chain_at = None
    b._thesis_dir = tmp
    b._thesis_arms = []
    b._thesis_closers = []
    b._thesis_seen = set()
    return b


def _write(tmp, obj, name=None):
    name = name or f"{obj.get('id', 'cmd')}.json"
    with open(os.path.join(tmp, name), 'w') as f:
        json.dump(obj, f)


def _dir_json(tmp):
    return [f for f in os.listdir(tmp) if f.endswith('.json')]


def _processed(tmp):
    p = os.path.join(tmp, 'processed')
    return sorted(os.listdir(p)) if os.path.isdir(p) else []


# ── 1. Pure: validation ──────────────────────────────────────────────────────

def test_validate():
    print("\nvalidate() — schema checks")
    ok, _ = commands.validate({'id': 'a', 'cmd': 'arm', 'side': 'CALL'})
    check("minimal arm (no trigger) is valid", ok)
    ok, _ = commands.validate({'id': 'a', 'cmd': 'arm', 'side': 'CALL',
                               'trigger': {'op': '>=', 'level': 7710, 'confirm_bars': 2}})
    check("arm with trigger is valid", ok)
    ok, err = commands.validate({'id': 'a', 'cmd': 'arm'})
    check("arm without side is rejected", not ok and 'side' in err)
    ok, err = commands.validate({'id': 'a', 'cmd': 'frobnicate'})
    check("unknown cmd is rejected", not ok)
    ok, err = commands.validate({'id': 'a', 'cmd': 'arm', 'side': 'CALL',
                                 'trigger': {'op': '!!', 'level': 1}})
    check("bad trigger op is rejected", not ok and 'op' in err)
    ok, err = commands.validate({'id': 'a', 'cmd': 'close_if'})
    check("close_if without 'when' is rejected", not ok)
    ok, err = commands.validate({'id': 'a', 'cmd': 'cancel'})
    check("cancel without cancel_id is rejected", not ok)
    ok, err = commands.validate({'id': 'a', 'cmd': 'arm', 'side': 'PUT',
                                 'expires_at': 'not-a-date'})
    check("unparseable expires_at is rejected", not ok)
    ok, _ = commands.validate({'_error': 'unparseable: x', 'id': 'a'})
    check("a malformed (_error) command is rejected", not ok)


# ── 2. Pure: trigger evaluation ──────────────────────────────────────────────

def test_triggers():
    print("\narm_should_fire() / closer_should_fire() / is_expired()")
    now_arm = {'trigger': None}
    check("no trigger → fire immediately", commands.arm_should_fire(now_arm, 7700, [7700]))

    call = {'trigger': {'op': '>=', 'level': 7710, 'confirm_bars': 1}}
    check(">= not met (7708) → no fire", not commands.arm_should_fire(call, 7708, [7705, 7708]))
    check(">= met (7712) → fire", commands.arm_should_fire(call, 7712, [7708, 7712]))

    call2 = {'trigger': {'op': '>=', 'level': 7710, 'confirm_bars': 2}}
    check("confirm_bars=2: only last bar past → no fire",
          not commands.arm_should_fire(call2, 7712, [7709, 7712]))
    check("confirm_bars=2: last TWO bars past → fire",
          commands.arm_should_fire(call2, 7713, [7711, 7713]))
    check("confirm_bars=2 but only 1 bar of data → no fire",
          not commands.arm_should_fire(call2, 7713, [7713]))

    put = {'trigger': {'op': '<=', 'level': 7700, 'confirm_bars': 1}}
    check("PUT <= met (7698) → fire", commands.arm_should_fire(put, 7698, [7702, 7698]))
    check("PUT <= not met (7701) → no fire", not commands.arm_should_fire(put, 7701, [7701]))

    closer = {'when': {'op': '<=', 'level': 7703}}
    check("closer <=7703 met at 7700", commands.closer_should_fire(closer, 7700))
    check("closer <=7703 not met at 7706", not commands.closer_should_fire(closer, 7706))

    now = market_time.now_et()
    check("no expires_at → never expired", not commands.is_expired({}, now))
    check("past expires_at → expired", commands.is_expired({'expires_at': '2020-01-01T00:00:00'}, now))
    check("future expires_at → not expired",
          not commands.is_expired({'expires_at': '2099-01-01T00:00:00'}, now))


# ── 3. Pure: scan + mark_processed round-trip ────────────────────────────────

def test_scan_and_processed():
    print("\nscan() + mark_processed() — filesystem lifecycle")
    tmp = tempfile.mkdtemp()
    try:
        _write(tmp, {'id': 'x1', 'cmd': 'arm', 'side': 'CALL'})
        with open(os.path.join(tmp, 'broken.json'), 'w') as f:
            f.write('{ this is not json')
        found = commands.scan(tmp)
        check("scan finds both files", len(found) == 2)
        broken = [c for c in found if c.get('_error')]
        check("malformed file flagged with _error", len(broken) == 1)
        good = [c for c in found if not c.get('_error')][0]
        dest = commands.mark_processed(good, tmp, 'fired')
        check("mark_processed returns a destination path", bool(dest))
        check("processed file left the scan dir", 'x1.json' not in _dir_json(tmp))
        check("processed file lives under processed/ with status prefix",
              any(f.startswith('fired-x1') for f in _processed(tmp)))
        check("re-scan no longer returns the processed arm",
              all(c.get('id') != 'x1' for c in commands.scan(tmp) if not c.get('_error')))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 4. Integration: arm fires only when the trigger is met ───────────────────

def test_arm_fires_on_trigger():
    print("\nintegration — arm fires only when spot crosses the level")
    tmp = tempfile.mkdtemp()
    try:
        b = _make_bot(tmp, spot=7705.0)
        _write(tmp, {'id': 'arm-call', 'cmd': 'arm', 'side': 'CALL',
                     'trigger': {'op': '>=', 'level': 7710, 'confirm_bars': 1},
                     'note': 'break > 7710'})
        b._process_thesis_commands()
        check("arm registered as pending", len(b._thesis_arms) == 1)
        b._watch_thesis_triggers()
        check("spot 7705 < 7710 → not fired", 'thesis:SPX' not in b.active_trades)
        check("arm file still present (not consumed)", 'arm-call.json' in _dir_json(tmp))

        b.broker.set_spot(7712.0)                       # breakout
        b._process_thesis_commands()
        b._watch_thesis_triggers()
        check("spot 7712 ≥ 7710 → thesis:SPX opened", 'thesis:SPX' in b.active_trades)
        t = b.active_trades.get('thesis:SPX', {})
        check("opened as a thesis single-leg CALL, pending entry",
              t.get('strategy') == 'thesis' and t.get('direction') == 'CALL'
              and t.get('status') == 'PENDING_ENTRY')
        check("fired arm moved to processed/", any(f.startswith('fired-arm-call') for f in _processed(tmp)))
        check("arm no longer pending in memory", len(b._thesis_arms) == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 5. Integration: immediate arm + already-holding guard ────────────────────

def test_immediate_and_stack_guard():
    print("\nintegration — no-trigger arm fires now; won't stack a 2nd thesis position")
    tmp = tempfile.mkdtemp()
    try:
        b = _make_bot(tmp, spot=7700.0)
        _write(tmp, {'id': 'buy-now', 'cmd': 'arm', 'side': 'PUT', 'note': 'now'})
        b._process_thesis_commands()
        b._watch_thesis_triggers()
        check("no-trigger arm fired immediately", 'thesis:SPX' in b.active_trades)

        _write(tmp, {'id': 'buy-now-2', 'cmd': 'arm', 'side': 'CALL', 'note': 'again'})
        b._process_thesis_commands()
        b._watch_thesis_triggers()
        check("second arm skipped (already holding thesis:SPX)",
              b.active_trades['thesis:SPX']['direction'] == 'PUT')
        check("skipped arm moved to processed/", any(f.startswith('skipped-buy-now-2') for f in _processed(tmp)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 6. Integration: close_if closes an ACTIVE position ───────────────────────

def test_close_if():
    print("\nintegration — close_if closes the ACTIVE thesis position on its condition")
    tmp = tempfile.mkdtemp()
    try:
        b = _make_bot(tmp, spot=7706.0)
        # Seed an ACTIVE thesis trade directly (as if the entry already filled)
        opt = FakeContract(7700)
        b.active_trades['thesis:SPX'] = {
            'strategy': 'thesis', 'symbol': 'SPX', 'structure': 'SINGLE', 'direction': 'CALL',
            'status': 'ACTIVE', 'entry_price': 9.5, 'qty': 1, 'option_contract': opt,
            'long_conid': opt.conId, 'leg_conids': [opt.conId], 'max_profit_pct': 0.0,
        }
        _write(tmp, {'id': 'stop-7703', 'cmd': 'close_if', 'when': {'op': '<=', 'level': 7703}})
        b._process_thesis_commands()
        check("close_if registered as pending", len(b._thesis_closers) == 1)
        b._watch_thesis_triggers()
        check("spot 7706 > 7703 → not closed yet",
              b.active_trades['thesis:SPX'].get('status') == 'ACTIVE')

        b.broker.set_spot(7701.0)                       # drop through the stop
        b._process_thesis_commands()
        b._watch_thesis_triggers()
        check("spot 7701 ≤ 7703 → close submitted (PENDING_EXIT)",
              b.active_trades['thesis:SPX'].get('status') == 'PENDING_EXIT')
        check("close_if consumed to processed/", any(f.startswith('closed-stop-7703') for f in _processed(tmp)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 7. Integration: cancel a pending arm ─────────────────────────────────────

def test_cancel():
    print("\nintegration — cancel drops a pending arm before it can fire")
    tmp = tempfile.mkdtemp()
    try:
        b = _make_bot(tmp, spot=7700.0)
        _write(tmp, {'id': 'arm-put', 'cmd': 'arm', 'side': 'PUT',
                     'trigger': {'op': '<=', 'level': 7000, 'confirm_bars': 1}})   # far, won't fire
        b._process_thesis_commands()
        check("arm pending", len(b._thesis_arms) == 1)
        _write(tmp, {'id': 'cancel-1', 'cmd': 'cancel', 'cancel_id': 'arm-put'})
        b._process_thesis_commands()
        b._watch_thesis_triggers()
        check("arm removed from pending", len(b._thesis_arms) == 0)
        check("no position opened", 'thesis:SPX' not in b.active_trades)
        check("arm file cancelled to processed/", any(f.startswith('cancelled-arm-put') for f in _processed(tmp)))
        check("cancel command itself consumed", any(f.startswith('done-cancel-1') for f in _processed(tmp)))
        check("scan dir clear of live json", _dir_json(tmp) == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 8. Integration: expiry + malformed rejection ─────────────────────────────

def test_expiry_and_reject():
    print("\nintegration — expired arm swept; malformed file rejected (no crash)")
    tmp = tempfile.mkdtemp()
    try:
        b = _make_bot(tmp, spot=7700.0)
        _write(tmp, {'id': 'stale', 'cmd': 'arm', 'side': 'CALL',
                     'trigger': {'op': '>=', 'level': 9999}, 'expires_at': '2020-01-01T00:00:00'})
        b._process_thesis_commands()
        b._watch_thesis_triggers()
        check("expired arm did not fire", 'thesis:SPX' not in b.active_trades)
        check("expired arm swept to processed/", any(f.startswith('expired-stale') for f in _processed(tmp)))

        with open(os.path.join(tmp, 'garbage.json'), 'w') as f:
            f.write('{ not valid')
        b._process_thesis_commands()          # must not raise
        check("malformed file rejected to processed/", any(f.startswith('rejected-garbage') for f in _processed(tmp)))
        check("scan dir clear after handling", _dir_json(tmp) == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    test_validate()
    test_triggers()
    test_scan_and_processed()
    test_arm_fires_on_trigger()
    test_immediate_and_stack_guard()
    test_close_if()
    test_cancel()
    test_expiry_and_reject()
    print()
    if _fails:
        print(f"❌ {len(_fails)} FAILED: {_fails}")
        sys.exit(1)
    print("✅ all thesis command-rail tests passed")
