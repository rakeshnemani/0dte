"""Trend-following backtest: Supertrend(7,3) + PSAR(0.02,0.2) — the user's idea.

Tests whether entering on a TREND START (Supertrend/PSAR flip) and exiting on the
flip back beats our breakout-follow (which enters late, at the top). Same instrument
(SPX 0DTE ATM debit spread), same underlying-move proxy as backtest_39, fees overlaid.

Variants (all follow the trend — no flip):
  CURRENT            our breakout entry (entry_signal), exit TP/STOP/EOD
  TREND-START        enter on Supertrend flip, exit on Supertrend flip back / TP / STOP / EOD
  TREND-START+PSAR   same, but require PSAR to agree at entry (fewer, cleaner)
  BREAKOUT+filter    our breakout entry, taken only if Supertrend agrees (user's Option B)

PROXY, fees EXCLUDED from gross then overlaid: TP +40bp(~+60% spread), STOP -55bp(~-70%),
FEE ~2 bp/trade (the real 08-07 $6.52 round-trip). Small sample, single-symbol SPY signal
source. READ-ONLY historical bars. clientId=16.
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import datetime, os, sys
import numpy as np
import pandas as pd
import pytz
import ta
from ib_insync import IB, Stock, util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
import config
import strategy

ET = pytz.timezone('America/New_York')
TP_BP, STOP_BP, FEE_BP = 40, -55, 2.0
COOLDOWN = datetime.timedelta(minutes=config.SIGNAL_COOLDOWN_MINUTES)

d0, d1 = datetime.date(2026, 6, 30), datetime.date(2026, 8, 7)
DAYS = []
d = d0
while d <= d1:
    if d.weekday() < 5:
        DAYS.append(d.isoformat())
    d += datetime.timedelta(days=1)


# ── indicators ────────────────────────────────────────────────────────────────
def supertrend_dir(df, period=7, mult=3.0):
    """+1 uptrend / -1 downtrend, standard Supertrend. NaN-guarded early bars = -0 (skip)."""
    hl2 = (df['high'].values + df['low'].values) / 2.0
    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'],
                                         window=period).average_true_range().values
    close = df['close'].values
    n = len(df)
    ub, lb = hl2 + mult * atr, hl2 - mult * atr
    fub = np.zeros(n); flb = np.zeros(n); st = np.zeros(n); d = np.zeros(n, dtype=int)
    for i in range(n):
        if i == 0 or np.isnan(atr[i]):
            fub[i], flb[i], st[i], d[i] = ub[i], lb[i], ub[i], 0
            continue
        fub[i] = ub[i] if (ub[i] < fub[i-1] or close[i-1] > fub[i-1]) else fub[i-1]
        flb[i] = lb[i] if (lb[i] > flb[i-1] or close[i-1] < flb[i-1]) else flb[i-1]
        if st[i-1] == fub[i-1]:
            st[i] = flb[i] if close[i] > fub[i] else fub[i]
        else:
            st[i] = fub[i] if close[i] < flb[i] else flb[i]
        d[i] = 1 if st[i] == flb[i] else -1
    return d


def psar_dir(df):
    ind = ta.trend.PSARIndicator(df['high'], df['low'], df['close'], step=0.02, max_step=0.2)
    up = ind.psar_up()                      # non-NaN when price is ABOVE PSAR (bullish)
    return np.where(up.notna().values, 1, -1)


def choppiness(df, n=14):
    """Choppiness Index (0-100): >~61 = choppy/whipsaw, <~38 = trending.
    100·log10(Σ TR over n / (maxHigh_n − minLow_n)) / log10(n)."""
    tr = pd.concat([df['high'] - df['low'],
                    (df['high'] - df['close'].shift()).abs(),
                    (df['low'] - df['close'].shift()).abs()], axis=1).max(axis=1)
    rng = df['high'].rolling(n).max() - df['low'].rolling(n).min()
    chop = 100 * np.log10(tr.rolling(n).sum() / rng.replace(0, np.nan)) / np.log10(n)
    return chop.values


# ── data ──────────────────────────────────────────────────────────────────────
ib = IB()
try:
    ib.connect('127.0.0.1', 4002, clientId=16, timeout=20)
except Exception as e:
    raise SystemExit(f"IB Gateway not reachable: {e}")

_cache = {}
def day_bars(day):
    if day in _cache:
        return _cache[day]
    raw = ib.reqHistoricalData(Stock('SPY', 'SMART', 'USD'),
                               endDateTime=day.replace('-', '') + ' 16:00:00 US/Eastern',
                               durationStr='1 D', barSizeSetting='1 min', whatToShow='TRADES',
                               useRTH=True, formatDate=1, timeout=40)
    if not raw:
        _cache[day] = None; return None
    df = util.df(raw).copy()
    df['date'] = pd.to_datetime(df['date'])
    df['date'] = (df['date'].dt.tz_localize('America/New_York') if df['date'].dt.tz is None
                  else df['date'].dt.tz_convert('America/New_York'))
    df = df.set_index('date')
    _cache[day] = df
    return df


def simulate(df, i, direction, exit_sdir=None):
    """Enter at bar i; exit on TP / STOP / (optional) Supertrend flip against us / EOD.
    Returns (win|loss, bp, exit_index)."""
    entry = float(df['close'].iloc[i]); sign = 1 if direction == 'CALL' else -1
    for j in range(i + 1, len(df)):
        bp = sign * (float(df['close'].iloc[j]) - entry) / entry * 1e4
        if bp >= TP_BP:
            return 'win', TP_BP, j
        if bp <= STOP_BP:
            return 'loss', STOP_BP, j
        if exit_sdir is not None:                       # exit when trend flips against us
            if (sign == 1 and exit_sdir[j] == -1) or (sign == -1 and exit_sdir[j] == 1):
                return ('win' if bp > 0 else 'loss'), bp, j
        t = df.index[j]
        if t.hour == 15 and t.minute >= 55:
            return ('win' if bp > 0 else 'loss'), bp, j
    last = sign * (float(df['close'].iloc[-1]) - entry) / entry * 1e4
    return ('win' if last > 0 else 'loss'), last, len(df) - 1


def run(name, mode, chop_max=None, days=None):
    trades = []
    for day in (days if days is not None else DAYS):
        df = day_bars(day)
        if df is None or len(df) < 40:
            continue
        sdir = supertrend_dir(df); pdir = psar_dir(df); chop = choppiness(df)
        cooldown = {}; open_until = -1
        for i in range(30, len(df)):
            if i <= open_until:
                continue
            t = df.index[i]; direction = None; exit_sdir = sdir
            if mode in ('ST', 'ST_PSAR'):
                if sdir[i] != 0 and sdir[i] != sdir[i-1]:          # Supertrend flip = trend start
                    # CHOP gate: only enter if the last 14 bars were TRENDING (low CHOP)
                    if chop_max is not None and not (chop[i] < chop_max):
                        continue
                    direction = 'CALL' if sdir[i] == 1 else 'PUT'
                    if mode == 'ST_PSAR' and pdir[i] != sdir[i]:   # require PSAR to agree
                        direction = None
            else:  # CURRENT (breakout-follow) or BREAKOUT_FILTER
                d, _, _, _ = strategy.entry_signal('SPX', df.iloc[:i+1].copy(), t)
                direction = d
                if mode == 'CURRENT':
                    exit_sdir = None                                # our exit = TP/STOP/EOD only
                elif mode == 'BREAKOUT_FILTER' and direction is not None:
                    want = 1 if direction == 'CALL' else -1
                    if sdir[i] != want:
                        direction = None
            if direction is None:
                continue
            if direction in cooldown and t < cooldown[direction]:
                continue
            outcome, bp, exit_i = simulate(df, i, direction, exit_sdir)
            trades.append((outcome, bp))
            cooldown[direction] = t + COOLDOWN; open_until = exit_i
    n = len(trades); w = sum(o == 'win' for o, _ in trades); g = sum(b for _, b in trades)
    net = g - FEE_BP * n
    wr = w / n * 100 if n else 0
    print(f"{name:<28}{n:>4} tr | {w}W/{n-w}L ({wr:>3.0f}%) | gross {g:>+6.0f} | net {net:>+6.0f} bp")
    return trades


# entry knobs for the breakout modes = current INITIAL config (filters off, follow)
config.ADX_SLOPE_BARS = 0; config.ORB_BREAKOUT_BUFFER_PCT = 0
config.PATH_FRESH_BARS = 0; config.PATH_MOMENTUM_BARS = 0; config.ADX_SLOPE_OVERRIDE_ADX = 0

H1 = DAYS[:len(DAYS) // 2]     # first ~15 weekdays
H2 = DAYS[len(DAYS) // 2:]     # last  ~14 weekdays

print(f"\nIN/OUT-OF-SAMPLE SPLIT — is 'TREND-START · CHOP<50' a real edge or a fit?\n"
      f"First half: {H1[0]}…{H1[-1]}  |  Second half: {H2[0]}…{H2[-1]}\n")
print("FULL (reference):")
run("  CHOP < 50", 'ST', chop_max=50)
print(f"\nFIRST HALF ({len(H1)} days):")
run("  no gate", 'ST', days=H1)
for thr in (55, 50, 45):
    run(f"  CHOP < {thr}", 'ST', chop_max=thr, days=H1)
print(f"\nSECOND HALF ({len(H2)} days):")
run("  no gate", 'ST', days=H2)
for thr in (55, 50, 45):
    run(f"  CHOP < {thr}", 'ST', chop_max=thr, days=H2)
print("\nREAL edge → CHOP<50 nets positive in BOTH halves AND the best threshold is stable.")
print("FIT → it works in one half only, or each half's sweet spot is a different threshold.")
ib.disconnect()
