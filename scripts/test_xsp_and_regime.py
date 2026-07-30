"""Unit tests for the XSP migration (#3) and regime-aware exit/entry (#32).

Pure logic only — no IBKR connection. Run:  python scripts/test_xsp_and_regime.py
Covers:
  #3  broker contract mapping (XSP index-option path vs SPY equity vs SPX), additive.
  #32 thesis_invalidated VWAP buffer + ADX trend-hold (and buf=0/hold=0 = old rule).
  #32 entry ADX-slope override (strong-but-flat ADX enters when override is set).
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())   # ib_insync needs a loop at import

import os
import sys
import datetime

import pandas as pd
import pytz

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import config
import strategy
from broker import IBKRBroker, INDEX_SPECS

ET = pytz.timezone('America/New_York')
_fails = []


def check(name, cond):
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}")
    if not cond:
        _fails.append(name)


# ── #3: broker contract mapping (no connection needed) ───────────────────────
def test_broker_contract_mapping():
    print("\n#3 — broker contract mapping (XSP additive, SPY/SPX preserved)")
    b = IBKRBroker()   # __init__ only builds IB(), does not connect

    check("option_symbol XSP → XSP", b.option_symbol('XSP') == 'XSP')
    check("option_symbol SPY → SPY", b.option_symbol('SPY') == 'SPY')
    check("option_symbol SPX → SPXW (preserved)", b.option_symbol('SPX') == 'SPXW')

    check("option_exchange XSP → CBOE", b.option_exchange('XSP') == 'CBOE')
    check("option_exchange SPY → SMART", b.option_exchange('SPY') == 'SMART')
    check("option_exchange SPX → SMART (preserved)", b.option_exchange('SPX') == 'SMART')

    xsp_u = b.underlying_contract('XSP')
    check("underlying XSP is an Index on CBOE",
          xsp_u.secType == 'IND' and xsp_u.exchange == 'CBOE')
    spy_u = b.underlying_contract('SPY')
    check("underlying SPY is a Stock on SMART",
          spy_u.secType == 'STK' and spy_u.exchange == 'SMART')

    xsp_bag = b.make_bag_multi('XSP', [(111, 'BUY'), (222, 'SELL')])
    check("XSP bag routes on CBOE (bag + all legs)",
          xsp_bag.exchange == 'CBOE'
          and all(leg.exchange == 'CBOE' for leg in xsp_bag.comboLegs))
    spy_bag = b.make_bag_multi('SPY', [(1, 'BUY'), (2, 'SELL')])
    check("SPY bag routes on SMART (preserved)",
          spy_bag.exchange == 'SMART'
          and all(leg.exchange == 'SMART' for leg in spy_bag.comboLegs))

    # #41: a BAG's symbol is the UNDERLYING, not the option root. SPX root is SPXW
    # but its BAG.symbol must be SPX (else IBKR error 478). XSP/SPY: root == underlying.
    check("SPX bag.symbol is underlying 'SPX', not root 'SPXW' (#41)",
          b.make_bag_multi('SPX', [(1, 'BUY'), (2, 'SELL')]).symbol == 'SPX')
    check("XSP bag.symbol == 'XSP'", xsp_bag.symbol == 'XSP')
    check("SPY bag.symbol == 'SPY'", spy_bag.symbol == 'SPY')

    # #42 fade-the-breakout: FLIP_DIRECTION inverts only CALL/PUT execution.
    import bot as _bot
    _saved = config.FLIP_DIRECTION
    try:
        config.FLIP_DIRECTION = False
        check("flip OFF: CALL stays CALL", _bot.TradingBot._maybe_flip('CALL') == 'CALL')
        config.FLIP_DIRECTION = True
        check("flip ON: CALL → PUT", _bot.TradingBot._maybe_flip('CALL') == 'PUT')
        check("flip ON: PUT → CALL", _bot.TradingBot._maybe_flip('PUT') == 'CALL')
        check("flip ON: CONDOR untouched", _bot.TradingBot._maybe_flip('CONDOR') == 'CONDOR')
    finally:
        config.FLIP_DIRECTION = _saved

    check("SIGNAL_SOURCE maps XSP → SPY", config.SIGNAL_SOURCE.get('XSP') == 'SPY')
    check("SIGNAL_SOURCE leaves SPY self-sourced", config.SIGNAL_SOURCE.get('SPY', 'SPY') == 'SPY')
    check("XSP is a known cash-settled index", 'XSP' in INDEX_SPECS)


# ── helpers to build synthetic 1-min bars ────────────────────────────────────
def _bars(prices):
    idx = [ET.localize(datetime.datetime(2026, 7, 20, 9, 30) + datetime.timedelta(minutes=i))
           for i in range(len(prices))]
    df = pd.DataFrame({'open': prices, 'high': prices, 'low': prices,
                       'close': prices, 'volume': [1000] * len(prices)}, index=pd.DatetimeIndex(idx))
    return df


def _set(**kw):
    saved = {k: getattr(config, k) for k in kw}
    for k, v in kw.items():
        setattr(config, k, v)
    return saved


def _restore(saved):
    for k, v in saved.items():
        setattr(config, k, v)


# ── #32: thesis_invalidated buffer ───────────────────────────────────────────
def test_invalidation_buffer():
    print("\n#32 — VWAP invalidation buffer (marginal recross should NOT invalidate)")
    # 19 flat bars at 100, then 6 bars a hair above (a PUT's wrong side is > VWAP).
    df = _bars([100.0] * 19 + [100.03] * 6)   # +0.03% recross
    saved = _set(VWAP_INVALIDATION_BARS=6, VWAP_INVALIDATION_HOLD_ADX=0,
                 VWAP_INVALIDATION_BUFFER_PCT=0.0)
    try:
        check("buf=0: a bare recross DOES invalidate (old behavior preserved)",
              strategy.thesis_invalidated('PUT', df) is True)
        config.VWAP_INVALIDATION_BUFFER_PCT = 0.0005   # 0.05% > the 0.03% recross
        check("buf=0.05%: the same marginal recross does NOT invalidate",
              strategy.thesis_invalidated('PUT', df) is False)
        # A decisive recross (0.20%) still invalidates even with the buffer.
        df_big = _bars([100.0] * 19 + [100.20] * 6)
        check("buf=0.05%: a decisive 0.20% recross STILL invalidates",
              strategy.thesis_invalidated('PUT', df_big) is True)
    finally:
        _restore(saved)


# ── #32: ADX trend-hold is DIRECTION-AWARE (review fix #2) ────────────────────
def _adx(df):
    return strategy.ta.trend.ADXIndicator(high=df['high'], low=df['low'],
                                          close=df['close'], window=14)

def test_invalidation_trend_hold():
    print("\n#32 — ADX trend-hold suppresses ONLY when the strong trend favors the trade")
    # Dominant DOWNtrend (DI− ≫ DI+, favors a PUT) with a single bar ticking back
    # ABOVE VWAP (wrong side for the PUT). n=1 so one counter-bar trips the raw rule.
    favor = _bars([140.0 - i * 0.8 for i in range(59)] + [100.0])   # DI− stays dominant
    # Same trend but a big counter-bar flips DI+ > DI− → the trend is now AGAINST the PUT.
    adverse = _bars([140.0 - i * 0.8 for i in range(59)] + [106.0])
    saved = _set(VWAP_INVALIDATION_BARS=1, VWAP_INVALIDATION_BUFFER_PCT=0.0,
                 VWAP_INVALIDATION_HOLD_ADX=0)
    try:
        f, a = _adx(favor), _adx(adverse)
        check("favorable frame: strong trend, DI− > DI+ (favors the PUT)",
              f.adx().iloc[-1] > 25 and f.adx_neg().iloc[-1] > f.adx_pos().iloc[-1])
        check("adverse frame: counter-bar flipped DI+ > DI− (trend against the PUT)",
              a.adx_pos().iloc[-1] > a.adx_neg().iloc[-1])
        check("hold=0: both raw-invalidate (old behavior preserved)",
              strategy.thesis_invalidated('PUT', favor) is True
              and strategy.thesis_invalidated('PUT', adverse) is True)
        config.VWAP_INVALIDATION_HOLD_ADX = float(f.adx().iloc[-1]) - 5.0
        check("hold + trend FAVORS the PUT → invalidation SUPPRESSED",
              strategy.thesis_invalidated('PUT', favor) is False)
        config.VWAP_INVALIDATION_HOLD_ADX = float(a.adx().iloc[-1]) - 5.0
        check("hold + trend AGAINST the PUT → STILL invalidates (fix #2: no adverse-trend hold)",
              strategy.thesis_invalidated('PUT', adverse) is True)
    finally:
        _restore(saved)


# ── #32: entry ADX-slope override ────────────────────────────────────────────
def test_entry_adx_override():
    print("\n#32 — entry ADX-slope override (strong-but-flat ADX can still enter)")
    # Steep decline (builds high ADX) then a GENTLE decline: ADX decelerates so
    # its 10-bar slope turns <= 0, while price still makes new lows (stays below
    # the rolling VWAP + ORB-low). The pure rising-ADX gate would block this.
    prices = [200.0 - i for i in range(41)] + [160.0 - (j + 1) * 0.05 for j in range(24)]
    df = _bars(prices)
    now = df.index[-1] + datetime.timedelta(minutes=1)
    saved = _set(ADX_SLOPE_BARS=10, ORB_BREAKOUT_BUFFER_PCT=0.001,
                 PATH_FRESH_BARS=0, PATH_MOMENTUM_BARS=0, ADX_SLOPE_OVERRIDE_ADX=0)
    try:
        d0, _, _, _ = strategy.entry_signal('XSP', df.copy(), now)
        check("override=0: strong-but-flat-ADX PUT is BLOCKED (pre-#32 behavior)",
              d0 is None)
        # Read the live ADX to set a passable override just under it.
        tmp = strategy.add_indicators(df.copy())
        live_adx = float(tmp['ADX'].iloc[-1])
        config.ADX_SLOPE_OVERRIDE_ADX = max(1.0, live_adx - 3.0)
        d1, reason, _, _ = strategy.entry_signal('XSP', df.copy(), now)
        check(f"override set (< live ADX {live_adx:.0f}): the SAME setup enters PUT",
              d1 == 'PUT')
    finally:
        _restore(saved)


if __name__ == '__main__':
    test_broker_contract_mapping()
    test_invalidation_buffer()
    test_invalidation_trend_hold()
    test_entry_adx_override()
    print()
    if _fails:
        print(f"❌ {len(_fails)} FAILED: {_fails}")
        sys.exit(1)
    print("✅ all XSP + regime-aware tests passed")
