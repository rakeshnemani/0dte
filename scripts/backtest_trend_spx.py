"""Trend/CHOP backtest on REAL SPX index bars (not the SPY proxy), longer history.

Pulls ~3 months of SPX 1-min RTH bars straight from IBKR and re-runs the candidate:
TREND-START (Supertrend 7,3 flip) + CHOP(14) < 50 gate, with an in/out-of-sample split.
Supertrend/PSAR/CHOP use only high/low/close, so no volume is needed — SPX index bars
are fine (and match the user's chart). Bigger, unseen sample = the real trust test.

Proxy P&L (TP +40bp≈+60% spread, STOP −55bp≈−70%), FEE ~2bp/trade overlaid. clientId=17.
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import datetime, os, sys
import numpy as np
import pandas as pd
import pytz
import ta
from ib_insync import IB, Index, util

TP_BP, STOP_BP, FEE_BP = 40, -55, 2.0
#COOLDOWN = datetime.timedelta(minutes=30)
COOLDOWN = datetime.timedelta(minutes=0)
ET = pytz.timezone('America/New_York')
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.spx_1min_cache.pkl')

# CLI:  python scripts/backtest_trend_spx.py [chop1 chop2 ...] [--refresh]
#   e.g.  python scripts/backtest_trend_spx.py 60 55 50 45   → test those CHOP gates
#         python scripts/backtest_trend_spx.py --refresh 50  → re-pull SPX bars, then test 50
# Bars are cached to disk on the first pull, so later runs are instant (no Gateway needed).
#         python scripts/backtest_trend_spx.py 55 50 --tf 5   → resample to 5-min bars
_args = [a for a in sys.argv[1:] if a != '--refresh']
REFRESH = '--refresh' in sys.argv[1:]
TF = 1
if '--tf' in _args:
    _k = _args.index('--tf'); TF = int(_args[_k + 1]); _args = _args[:_k] + _args[_k + 2:]
THRESHOLDS = [float(a) for a in _args] or [38.5]     # Kaufman chop gate (low = trending)
START = max(15, 30 // TF)     # session warmup; also ensures CHOP(14)/Supertrend are valid


def supertrend_dir(df, period=7, mult=3.0):
    hl2 = (df['high'].values + df['low'].values) / 2.0
    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=period).average_true_range().values
    close = df['close'].values; n = len(df)
    ub, lb = hl2 + mult * atr, hl2 - mult * atr
    fub = np.zeros(n); flb = np.zeros(n); st = np.zeros(n); d = np.zeros(n, dtype=int)
    for i in range(n):
        if i == 0 or np.isnan(atr[i]):
            fub[i], flb[i], st[i], d[i] = ub[i], lb[i], ub[i], 0; continue
        fub[i] = ub[i] if (ub[i] < fub[i-1] or close[i-1] > fub[i-1]) else fub[i-1]
        flb[i] = lb[i] if (lb[i] > flb[i-1] or close[i-1] < flb[i-1]) else flb[i-1]
        st[i] = (flb[i] if close[i] > fub[i] else fub[i]) if st[i-1] == fub[i-1] \
                else (fub[i] if close[i] < flb[i] else flb[i])
        d[i] = 1 if st[i] == flb[i] else -1
    return d


def kaufman_chop(df, n=14):
    """Kaufman chop factor = 100·(1 − Efficiency Ratio). 0 = clean trend, 100 = pure chop.
    ER = |net move over n bars| / Σ|bar-to-bar move over n bars|. LOW = trending, so the
    gate 'kaufman_chop < 38.5' trades only when the last n bars were efficient (ER > ~0.62)."""
    close = df['close']
    net = close.diff(n).abs()
    path = close.diff().abs().rolling(n).sum()
    er = (net / path.replace(0, np.nan)).clip(lower=0, upper=1)
    return (100 * (1 - er)).values


def psar_dir(df):
    """+1 when price is ABOVE the PSAR (bullish), −1 when below (bearish). (0.02,0.2)."""
    ind = ta.trend.PSARIndicator(df['high'], df['low'], df['close'], step=0.02, max_step=0.2)
    return np.where(ind.psar_up().notna().values, 1, -1)


def simulate(df, i, direction, pdir):
    """Exit on: max profit / max loss / PSAR flip against us ('SAR met') / EOD."""
    entry = float(df['close'].iloc[i]); sign = 1 if direction == 'CALL' else -1
    for j in range(i + 1, len(df)):
        bp = sign * (float(df['close'].iloc[j]) - entry) / entry * 1e4
        if bp >= TP_BP: return 'win', TP_BP, j
        if bp <= STOP_BP: return 'loss', STOP_BP, j
        if (sign == 1 and pdir[j] == -1) or (sign == -1 and pdir[j] == 1):   # PSAR flipped → exit
            return ('win' if bp > 0 else 'loss'), bp, j
        t = df.index[j]
        if t.hour == 15 and t.minute >= 55:
            return ('win' if bp > 0 else 'loss'), bp, j
    last = sign * (float(df['close'].iloc[-1]) - entry) / entry * 1e4
    return ('win' if last > 0 else 'loss'), last, len(df) - 1


# ── fetch real SPX 1-min RTH bars in chunks ──────────────────────────────────
ib = IB()
if os.path.exists(CACHE) and not REFRESH:
    df = pd.read_pickle(CACHE)
    print(f"Loaded cached SPX bars ({len(df)} bars, {df.index[0].date()}…{df.index[-1].date()}). "
          f"Use --refresh to re-pull from IBKR for newer days.")
else:
    ib = IB()
    try:
        ib.connect('127.0.0.1', 4002, clientId=17, timeout=20)
    except Exception as e:
        raise SystemExit(f"IB Gateway not reachable (needed to build the cache): {e}")
    spx = Index('SPX', 'CBOE', 'USD')
    ib.qualifyContracts(spx)

    def fetch(what):
        seen = {}; end = ''
        for _ in range(12):                   # ~12 × 10 calendar days ≈ 3+ months
            raw = ib.reqHistoricalData(spx, endDateTime=end, durationStr='10 D',
                                       barSizeSetting='1 min', whatToShow=what,
                                       useRTH=True, formatDate=1, timeout=60)
            if not raw:
                break
            for b in raw:
                seen[b.date] = b
            end = raw[0].date
            ib.sleep(1)                       # pacing
        return seen

    seen = fetch('TRADES') or fetch('MIDPOINT')
    ib.disconnect()
    if not seen:
        raise SystemExit("No SPX historical bars returned (index data may not be subscribed).")
    df = util.df(sorted(seen.values(), key=lambda b: b.date)).copy()
    df['date'] = pd.to_datetime(df['date'])
    df['date'] = (df['date'].dt.tz_localize('America/New_York') if df['date'].dt.tz is None
                  else df['date'].dt.tz_convert('America/New_York'))
    df = df.set_index('date')
    df.to_pickle(CACHE)
    print(f"Pulled + cached SPX bars → {CACHE} ({len(df)} bars).")

def _resample(g):
    """Aggregate 1-min OHLC → TF-min bars, within the day (no cross-session bleed)."""
    if TF <= 1:
        return g
    return g.resample(f'{TF}min', label='left', closed='left').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna(subset=['close'])

by_day = {d: _resample(g) for d, g in df.groupby(df.index.date) if len(g) >= 40}
by_day = {d: g for d, g in by_day.items() if len(g) >= START + 5}
DAYS = sorted(by_day.keys())
print(f"\nSPX {TF}-min bars: {len(DAYS)} trading days, {DAYS[0]} … {DAYS[-1]} "
      f"(resampled from {len(df)} 1-min bars; ~{sum(len(by_day[d]) for d in DAYS)//len(DAYS)} bars/day)")


def run(name, chop_max=None, days=None):
    trades = []
    for day in (days if days is not None else DAYS):
        d = by_day[day]
        sdir = supertrend_dir(d); pdir = psar_dir(d); kchop = kaufman_chop(d)
        cooldown = {}; open_until = -1
        for i in range(START, len(d)):
            if i <= open_until: continue
            if not (sdir[i] != 0 and sdir[i] != sdir[i-1]): continue      # Supertrend flip = trend start
            if pdir[i] != sdir[i]: continue                              # AND PSAR agrees
            if chop_max is not None and not (kchop[i] < chop_max): continue   # Kaufman chop gate
            direction = 'CALL' if sdir[i] == 1 else 'PUT'
            t = d.index[i]
            if direction in cooldown and t < cooldown[direction]: continue
            o, bp, ex = simulate(d, i, direction, pdir)                   # exit on PSAR flip
            trades.append((o, bp)); cooldown[direction] = t + COOLDOWN; open_until = ex
    n = len(trades); w = sum(o == 'win' for o, _ in trades); g = sum(b for _, b in trades)
    print(f"{name:<26}{n:>4} tr | {w}W/{n-w}L ({w/n*100 if n else 0:>3.0f}%) | "
          f"gross {g:>+6.0f} | net {g - FEE_BP*n:>+6.0f} bp")


H1, H2 = DAYS[:len(DAYS)//2], DAYS[len(DAYS)//2:]
print(f"\n=== FULL SAMPLE ({len(DAYS)} days, {TF}-min bars) — proxy P&L, fee {FEE_BP:g}bp/trade ===")
run("no gate")
for thr in THRESHOLDS:
    run(f"kauf-chop < {thr:g}", chop_max=thr)
print(f"\n=== IN/OUT-OF-SAMPLE SPLIT ===")
print(f"  first half {H1[0]}…{H1[-1]}  |  second half {H2[0]}…{H2[-1]}")
for thr in THRESHOLDS:
    print(f"  -- kauf-chop < {thr:g} --")
    run("     first half", chop_max=thr, days=H1)
    run("     second half", chop_max=thr, days=H2)
print(f"\nKaufman-chop gates: {', '.join(f'{t:g}' for t in THRESHOLDS)} (low=trending)  |  timeframe: {TF}-min.")
print("Change either:  python scripts/backtest_trend_spx.py 40 38.5 35 --tf 5   (kauf gates + 5-min bars)")
print("Real edge → the SAME threshold nets positive in BOTH halves. (--refresh re-pulls SPX bars.)")
