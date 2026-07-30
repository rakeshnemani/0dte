"""#39 what-if: does un-blocking trend-day entries raise the win rate?

Replays the strategy's SIGNAL GENERATION minute-by-minute over historical SPY bars
(the signal source), modelling the bot's gates (one position per symbol at a time,
30-min per-direction cooldown, MIN_CONVICTION_SCORE), then simulates each entry's
outcome with the real exit rule. Two configs:

  BASELINE  — current guards:  ADX_SLOPE_OVERRIDE_ADX=0, PATH_FRESH_BARS=10
  FIX39     — #39 proxy:        ADX_SLOPE_OVERRIDE_ADX=40, PATH_FRESH_BARS=0
              (freshness OFF but MOMENTUM check kept on — a mild upper bound on
               the real *selective* freshness waiver)

Exit config is production (N=6, no buffer/hold) for BOTH — #39 is entry-only.

PROXY, NOT REAL P&L: outcome = direction-adjusted underlying move with TP/STOP
proxies (win-cap +40bp≈+60% spread, loss-cap -55bp≈-70%); COMMISSIONS EXCLUDED.
Small sample, mixed regime. Directional estimate only.  READ-ONLY. clientId=15.
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import datetime
import os
import sys

import pandas as pd
import pytz
from ib_insync import IB, Stock, util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
import config
import strategy

ET = pytz.timezone('America/New_York')
TP_BP, STOP_BP = 40, -55
COOLDOWN = datetime.timedelta(minutes=config.SIGNAL_COOLDOWN_MINUTES)

# Era trading days = weekdays 2026-06-30 .. 2026-07-27 (day_bars returns None on holidays)
d0, d1 = datetime.date(2026, 6, 30), datetime.date(2026, 7, 27)
DAYS = []
d = d0
while d <= d1:
    if d.weekday() < 5:
        DAYS.append(d.isoformat())
    d += datetime.timedelta(days=1)

ib = IB()
try:
    ib.connect('127.0.0.1', 4002, clientId=15, timeout=20)
except Exception as e:
    raise SystemExit(f"IB Gateway not reachable: {e}")

_cache = {}
def day_bars(day):
    if day in _cache:
        return _cache[day]
    end = day.replace('-', '') + ' 16:00:00 US/Eastern'
    raw = ib.reqHistoricalData(Stock('SPY', 'SMART', 'USD'), endDateTime=end,
                               durationStr='1 D', barSizeSetting='1 min',
                               whatToShow='TRADES', useRTH=True, formatDate=1, timeout=40)
    if not raw:
        _cache[day] = None
        return None
    df = util.df(raw).copy()
    df['date'] = pd.to_datetime(df['date'])
    df['date'] = (df['date'].dt.tz_localize('America/New_York') if df['date'].dt.tz is None
                  else df['date'].dt.tz_convert('America/New_York'))
    df = df.set_index('date')
    _cache[day] = df
    return df


def simulate(df, i, direction):
    """Outcome of entering at bar i, exited by the real rule / TP-STOP proxy.
    Returns (win|loss, bp, exit_index)."""
    entry_px = float(df['close'].iloc[i])
    sign = 1 if direction == 'CALL' else -1
    for j in range(i + 1, len(df)):
        move_bp = sign * (float(df['close'].iloc[j]) - entry_px) / entry_px * 1e4
        if move_bp >= TP_BP:
            return 'win', TP_BP, j
        if move_bp <= STOP_BP:
            return 'loss', STOP_BP, j
        if strategy.thesis_invalidated(direction, df.iloc[:j + 1]):
            return ('win' if move_bp > 0 else 'loss'), move_bp, j
        t = df.index[j]
        if t.hour == 15 and t.minute >= 55:
            return ('win' if move_bp > 0 else 'loss'), move_bp, j
    last = sign * (float(df['close'].iloc[-1]) - entry_px) / entry_px * 1e4
    return ('win' if last > 0 else 'loss'), last, len(df) - 1


FLIP = {'CALL': 'PUT', 'PUT': 'CALL'}

def backtest(flip=False):
    trades = []
    for day in DAYS:
        df = day_bars(day)
        if df is None or len(df) < 40:
            continue
        cooldown = {}          # direction -> datetime until blocked
        open_until = -1        # bar index a position stays open through
        for i in range(30, len(df)):
            if i <= open_until:
                continue
            t = df.index[i]
            # One window object: entry_signal adds VWAP/ADX in place, then
            # conviction_score reads them off the same frame (as the bot does).
            window = df.iloc[:i + 1].copy()
            direction, _, ind, _ = strategy.entry_signal('SPX', window, t)
            if direction is None:
                continue
            if direction in cooldown and t < cooldown[direction]:
                continue
            conv = strategy.conviction_score('SPX', direction, window, ind, t, {}, 0)
            if conv['score'] < config.MIN_CONVICTION_SCORE:
                continue
            exec_dir = FLIP[direction] if flip else direction
            outcome, bp, exit_i = simulate(df, i, exec_dir)
            trades.append((day, t.strftime('%H:%M'), exec_dir, outcome, bp))
            cooldown[direction] = t + COOLDOWN
            open_until = exit_i
    return trades


def regime_at(df, i):
    """Descriptive regime from bars up to the entry (NO look-ahead), via crosses of
    the SESSION VWAP: a trend stays one side of it, chop oscillates across it."""
    sub = df.iloc[:i + 1]
    tp = (sub['high'] + sub['low'] + sub['close']) / 3
    svwap = (tp * sub['volume']).cumsum() / sub['volume'].cumsum().replace(0, 1)
    above = sub['close'] > svwap
    crosses = int((above != above.shift()).iloc[1:].sum())
    return ('CHOP' if crosses >= 8 else 'TREND'), crosses


def regime_split():
    """For each BASELINE (filtered) entry: tag its entry-bar regime, and record BOTH
    the as-taken and the flipped outcome. Answers: does flipping win in CHOP and lose
    in TREND? (the premise the chop→flip / trend→follow router depends on)."""
    config.ADX_SLOPE_BARS = 10; config.ADX_SLOPE_OVERRIDE_ADX = 0
    config.ORB_BREAKOUT_BUFFER_PCT = 0.001
    config.PATH_FRESH_BARS = 10; config.PATH_MOMENTUM_BARS = 3
    config.MIN_CONVICTION_SCORE = 2
    config.VWAP_INVALIDATION_BARS = 6
    config.VWAP_INVALIDATION_BUFFER_PCT = 0.0; config.VWAP_INVALIDATION_HOLD_ADX = 0.0
    rec = {'CHOP': [], 'TREND': []}
    for day in DAYS:
        df = day_bars(day)
        if df is None or len(df) < 40:
            continue
        cooldown = {}; open_until = -1
        for i in range(30, len(df)):
            if i <= open_until:
                continue
            t = df.index[i]; window = df.iloc[:i + 1].copy()
            d, _, ind, _ = strategy.entry_signal('SPX', window, t)
            if d is None:
                continue
            if d in cooldown and t < cooldown[d]:
                continue
            conv = strategy.conviction_score('SPX', d, window, ind, t, {}, 0)
            if conv['score'] < config.MIN_CONVICTION_SCORE:
                continue
            reg, _ = regime_at(df, i)
            o_t, bp_t, exit_i = simulate(df, i, d)
            o_f, bp_f, _ = simulate(df, i, FLIP[d])
            rec[reg].append((bp_t, o_t, bp_f, o_f))
            cooldown[d] = t + COOLDOWN; open_until = exit_i
    print(f"\n{'regime':<8}{'n':>4}   {'AS-TAKEN (follow)':<26}{'FLIPPED (fade)':<26}")
    for reg in ('CHOP', 'TREND'):
        r = rec[reg]; n = len(r)
        if not n:
            print(f"{reg:<8}{0:>4}   (no entries)"); continue
        tw, tbp = sum(x[1] == 'win' for x in r), sum(x[0] for x in r)
        fw, fbp = sum(x[3] == 'win' for x in r), sum(x[2] for x in r)
        print(f"{reg:<8}{n:>4}   {tw}W/{n-tw}L {tw/n*100:>3.0f}% {tbp:>+5.0f}bp          "
              f"{fw}W/{n-fw}L {fw/n*100:>3.0f}% {fbp:>+5.0f}bp")
    print("  premise holds IF: CHOP → flipped beats follow; TREND → follow beats flipped.")


def run(name, *, adx_slope_bars=10, orb_buffer=0.001, fresh_bars=10,
        momentum_bars=3, inval_bars=6, min_conviction=2, flip=False):
    """All entry/exit filters exposed. 'Initial only' = turn the added ones off.
    (ADX>25 and the base VWAP+ORB breakout are hardwired in entry_signal — the
    true original core — and stay in every scenario.)"""
    config.ADX_SLOPE_BARS = adx_slope_bars          # rising-ADX gate (added 07-01)
    config.ADX_SLOPE_OVERRIDE_ADX = 0.0
    config.ORB_BREAKOUT_BUFFER_PCT = orb_buffer     # breakout buffer (added 07-01)
    config.PATH_FRESH_BARS = fresh_bars             # #31 path guard (added 07-10)
    config.PATH_MOMENTUM_BARS = momentum_bars
    config.VWAP_INVALIDATION_BARS = inval_bars      # invalidation exit (added ~07-05)
    config.VWAP_INVALIDATION_BUFFER_PCT = 0.0
    config.VWAP_INVALIDATION_HOLD_ADX = 0.0
    config.MIN_CONVICTION_SCORE = min_conviction    # conviction gate (added 07-06)
    trades = backtest(flip=flip)
    wins = sum(1 for *_, o, _ in trades if o == 'win')
    n = len(trades)
    bp = sum(t[-1] for t in trades)
    wr = wins / n * 100 if n else 0
    print(f"{name:<40} {n:>3} trades | {wins}W/{n-wins}L ({wr:.0f}% win) | {bp:>+6.0f} bp")
    return trades


OFF = dict(adx_slope_bars=0, orb_buffer=0.0, fresh_bars=0, momentum_bars=0, min_conviction=-99)

print(f"\nInitial-vs-current backtest — {len(DAYS)} weekdays, SPY signal bars, "
      f"proxy P&L (fees excluded)\n")
run("BASELINE (all current filters)")
run("INITIAL ONLY (base entry, no invalidation)", inval_bars=0, **OFF)
print("\n— the real profit test: INITIAL ONLY, direction FLIPPED —")
run("INITIAL ONLY  FLIPPED", inval_bars=0, flip=True, **OFF)
run("BASELINE      FLIPPED", flip=True)
print("\n  (SPX fee hurdle ≈ ~2 bp/trade; a book must clear its trade-count × that to net positive)")

print("\n=== PREMISE TEST: does regime separate flip outcomes? (baseline entries) ===")
regime_split()

print("\nProxy: win-cap +40bp, loss-cap -55bp; fees EXCLUDED. 'Initial only' turns off every "
      "filter we added after 06-30 (slope gate, ORB buffer, #31 path guard, invalidation, "
      "conviction); ADX>25 + VWAP/ORB breakout are the hardwired original core.")
ib.disconnect()
