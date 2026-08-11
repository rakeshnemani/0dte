"""Unit tests for running trend + GEX at once (composite strategy:symbol trade keys).

Verifies the safety-critical bit: two SPX positions coexist, each tracked by its own
conId, and the orphan/anti-cascade guards gather conIds ACROSS both strategies so
neither ever mistakes the other's leg for an untracked orphan (the user's concern).

No IBKR — broker + positions are faked. Run:  python scripts/test_dual_strategy.py
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import audit
import notifier
audit.record = lambda *a, **k: None
for _fn in ('notify_submit', 'notify_today_summary', 'notify_untracked_holding'):
    setattr(notifier, _fn, lambda *a, **k: None)

import config
_fails = []


def check(name, cond):
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}")
    if not cond:
        _fails.append(name)


class FakeContract:
    def __init__(self, conid, sec='OPT', symbol='SPXW'):
        self.conId = conid; self.secType = sec; self.symbol = symbol
        self.right = 'C'; self.strike = conid


class FakePos:
    def __init__(self, conid, pos=1):
        self.contract = FakeContract(conid); self.position = pos


class FakeIbkrTrade:
    class _O: orderId = 7
    def __init__(self): self.order = self._O()


class FakeBroker:
    def __init__(self): self.positions_list = []
    def get_option_contract(self, symbol, direction, strike):
        return FakeContract(int(strike))          # conId == strike → trend/gex get distinct ids
    def _get_option_quote(self, opt): return (9.0, 9.5, 10.0)
    def _get_option_mid(self, opt): return 9.5
    def option_tick(self, symbol): return 0.05
    def option_symbol(self, symbol): return 'SPXW'
    def place_limit(self, *a, **k): return FakeIbkrTrade()
    def positions(self): return self.positions_list


def _make_bot():
    import bot as bot_mod
    b = bot_mod.TradingBot.__new__(bot_mod.TradingBot)
    b.active_trades = {}; b.closed_trades_today = []; b.broker = FakeBroker()
    return b


def test_dual():
    print("\ntwo strategies, one symbol — independent tracking")
    b = _make_bot()
    b._place_single_leg('SPX', 'CALL', 6000, {'current_price': 6000.0}, 'trend sig', 'trend')
    b._place_single_leg('SPX', 'PUT', 6010, {'current_price': 6010.0}, 'gex sig', 'gex')
    check("both positions tracked (trend:SPX + gex:SPX)",
          'trend:SPX' in b.active_trades and 'gex:SPX' in b.active_trades)
    check("distinct conIds (6000 vs 6010)",
          b.active_trades['trend:SPX']['long_conid'] == 6000
          and b.active_trades['gex:SPX']['long_conid'] == 6010)
    check("each tagged with its strategy",
          b.active_trades['trend:SPX']['strategy'] == 'trend'
          and b.active_trades['gex:SPX']['strategy'] == 'gex')

    print("\norphan guard is strategy-aware (gathers conIds across BOTH)")
    # account holds both legs → neither is 'untracked'
    b.broker.positions_list = [FakePos(6000), FakePos(6010)]
    check("both held → no untracked orphan", b._symbol_has_untracked_position('SPX') is False)
    check("trend position still open (its conId held)",
          b._position_still_open(b.active_trades['trend:SPX']) is True)
    check("gex position still open (its conId held)",
          b._position_still_open(b.active_trades['gex:SPX']) is True)

    # gex leg vanishes (closed externally) — trend must NOT be affected, no false orphan
    b.broker.positions_list = [FakePos(6000)]
    check("after gex leg gone: trend still open", b._position_still_open(b.active_trades['trend:SPX']) is True)
    check("after gex leg gone: gex reads closed", b._position_still_open(b.active_trades['gex:SPX']) is False)
    check("remaining held leg (trend's) is NOT flagged as orphan",
          b._symbol_has_untracked_position('SPX') is False)

    # a genuinely untracked leg appears → guard fires
    b.broker.positions_list = [FakePos(6000), FakePos(9999)]
    check("a truly untracked leg (9999) IS flagged", b._symbol_has_untracked_position('SPX') is True)


if __name__ == '__main__':
    test_dual()
    print()
    if _fails:
        print(f"❌ {len(_fails)} FAILED: {_fails}")
        sys.exit(1)
    print("✅ all dual-strategy tests passed")
