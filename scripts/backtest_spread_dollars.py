"""Real-DOLLAR spread backtest: 1 contract, actual option legs priced (not a bp proxy).

Why this exists: our other backtests measure P&L in basis points of the UNDERLYING
move — a proxy that cannot tell a $5 spread from a $10 one (width changes the legs'
cost, delta, and profit cap, none of which the bp-proxy sees). To compare widths and
report real dollars, this script prices BOTH option legs with Black-Scholes, with
theta decaying to the 4pm bell and vol calibrated from EACH DAY's own 1-min tape.

Strategy (the user's trend spec):
  ENTRY  Supertrend(7,3) flips (trend forms) AND PSAR(0.02,0.2) agrees
         AND kaufman-chop <= --kauf (default 50)  AND  entry time < --cutoff (default 14:00 ET)
  EXIT   trend reversed (PSAR flips against us)  ·  stop loss  ·  max profit  ·  EOD 15:55

1 contract, real IBKR fee (~$6.52 round-trip/contract). Reads a cached SPX 1-min pickle
(default: the 2-year cache if present, else the 120-day one). Writes EVERY trade to a CSV
(--csv) for hands-on pattern-hunting, and prints a per-QUARTER summary.

  python scripts/backtest_spread_dollars.py --width 10               # $10, quarterly, full CSV
  python scripts/backtest_spread_dollars.py --width 10 --kauf 45 --cutoff 13
  python scripts/backtest_spread_dollars.py --width 10 --ivmult 1.2  # harsher (realistic) theta
"""
import argparse
import os
import warnings
from math import log, sqrt, erf, exp, pi

warnings.filterwarnings('ignore')          # ta's PSAR spams a pandas FutureWarning
import numpy as np
import pandas as pd
import ta

# ── constants ─────────────────────────────────────────────────────────────────
QTY = 1
MULT = 100                       # SPX option contract multiplier ($/point)
FEE_RT = 6.52                    # real round-trip fee for a 1-lot 2-leg spread (08-07)
STRIKE_STEP = 5                  # SPX lists 5-pt strikes ATM
MIN_PER_YEAR = 252 * 390         # trading-minute clock (self-consistent vol ↔ theta)
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_2Y = os.path.join(HERE, '.spx_1min_2y_cache.pkl')
CACHE_120 = os.path.join(HERE, '.spx_1min_cache.pkl')

p = argparse.ArgumentParser()
p.add_argument('--width', type=float, help='spread width in $ (default: compare 5 and 10)')
p.add_argument('--kauf', type=float, default=50.0, help='kaufman-chop gate, enter only if <= (default 50)')
p.add_argument('--cutoff', type=float, default=14.0, help='no NEW entries at/after this ET hour (default 14 = 2PM)')
p.add_argument('--windows', help='only enter inside these ET windows, e.g. 09:30-10:00,15:00-16:00 (overrides --cutoff)')
p.add_argument('--tp', type=float, default=0.60, help='max-profit exit, %% of debit paid (default 0.60)')
p.add_argument('--stop', type=float, default=0.50, help='stop-loss exit, %% of debit paid (default 0.50)')
p.add_argument('--ivmult', type=float, default=1.0, help='scale the per-day realized vol used as IV (default 1.0)')
p.add_argument('--reverse', choices=['psar', 'supertrend'], default='psar',
               help="which 'trend reversed' exit: psar (whippy, ~9min) or supertrend (slow, lets winners run)")
p.add_argument('--legs', choices=['spread', 'single'], default='spread',
               help="'spread' = ATM debit vertical; 'single' = buy one ATM leg (half fee, uncapped, more theta)")
p.add_argument('--no-tp', action='store_true', help='disable the take-profit exit (ride to reversal/stop/EOD)')
p.add_argument('--skip-lowiv', type=float, default=0.0,
               help='skip entry when entry-time realized vol (open→now, annualized — NO lookahead) < this (0=off)')
p.add_argument('--trail-trigger', type=float, default=0.0,
               help='arm a trailing stop once profit peaks at this %% of cost, e.g. 0.70 (0=off)')
p.add_argument('--trail-giveback', type=float, default=0.20,
               help='once armed, exit if profit falls this fraction below the peak (0.20 = give back 20%% of peak)')
p.add_argument('--cache', help='path to a 1-min SPX pickle (default: 2y cache if present, else 120d)')
p.add_argument('--csv', default=os.path.join(HERE, 'trades_spread.csv'), help='per-trade output CSV')
A = p.parse_args()
WIDTHS = [A.width] if A.width else [5.0, 10.0]
# Single leg = 2 transactions round-trip vs the spread's 4 → half the fee. Width is
# irrelevant for a single leg, so run once.
FEE = round(FEE_RT / 2, 2) if A.legs == 'single' else FEE_RT
if A.legs == 'single':
    WIDTHS = [0.0]


# ── Black-Scholes (r=0; index, cash-settled) ──────────────────────────────────
def _ncdf(x): return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _bs(S, K, T, sig, call):
    if T <= 0 or sig <= 0:
        return max(S - K, 0.0) if call else max(K - S, 0.0)
    d1 = (log(S / K) + 0.5 * sig * sig * T) / (sig * sqrt(T))
    d2 = d1 - sig * sqrt(T)
    return (S * _ncdf(d1) - K * _ncdf(d2)) if call else (K * _ncdf(-d2) - S * _ncdf(-d1))


def spread_value(S, K, T, sig, width, call):
    if call:
        return _bs(S, K, T, sig, True) - _bs(S, K + width, T, sig, True)
    return _bs(S, K, T, sig, False) - _bs(S, K - width, T, sig, False)


def position_value(S, K, T, sig, width, call):
    """Value of the traded structure: a single ATM leg (--legs single) or the
    ATM debit spread (--legs spread), in option points."""
    if A.legs == 'single':
        return _bs(S, K, T, sig, call)
    return spread_value(S, K, T, sig, width, call)


def _npdf(x):
    return exp(-0.5 * x * x) / sqrt(2.0 * pi)


def _leg_greeks(S, K, T, sig, call):
    """Per-share (delta, gamma, theta_per_year, vega) for one option leg. r=0."""
    if T <= 0 or sig <= 0:
        d = (1.0 if S > K else 0.0) if call else (-1.0 if S < K else 0.0)
        return d, 0.0, 0.0, 0.0
    d1 = (log(S / K) + 0.5 * sig * sig * T) / (sig * sqrt(T))
    delta = _ncdf(d1) if call else _ncdf(d1) - 1.0
    gamma = _npdf(d1) / (S * sig * sqrt(T))
    theta = -S * _npdf(d1) * sig / (2.0 * sqrt(T))       # r=0 → only the time-decay term
    vega = S * _npdf(d1) * sqrt(T)
    return delta, gamma, theta, vega


def position_greeks(S, K, T, sig, width, call):
    """Greeks of the traded structure at entry — the single long leg (--legs single)
    or the NET of the debit spread (--legs spread). Trade-friendly units, 1 contract."""
    lo = _leg_greeks(S, K, T, sig, call)
    if A.legs == 'single':
        delta, gamma, theta, vega = lo
    else:
        hi = _leg_greeks(S, K + width if call else K - width, T, sig, call)
        delta, gamma, theta, vega = (lo[i] - hi[i] for i in range(4))
    return {
        'delta': round(delta, 3),                        # net directional exposure (per share)
        'gamma': round(gamma, 4),
        'theta_$day': round(theta / 252.0 * MULT, 1),    # $ decay per trading day (negative)
        'vega_$': round(vega * 0.01 * MULT, 1),          # $ per +1 IV point
    }


# ── indicators (high/low/close only → SPX index bars are fine) ────────────────
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


def psar_dir(df):
    ind = ta.trend.PSARIndicator(df['high'], df['low'], df['close'], step=0.02, max_step=0.2)
    return np.where(ind.psar_up().notna().values, 1, -1)


def kaufman_chop(df, n=14):
    close = df['close']
    net = close.diff(n).abs()
    path = close.diff().abs().rolling(n).sum()
    er = (net / path.replace(0, np.nan)).clip(lower=0, upper=1)
    return (100 * (1 - er)).values


def day_iv(df):
    lr = np.diff(np.log(df['close'].values))
    sig = np.nanstd(lr) * sqrt(MIN_PER_YEAR) * A.ivmult
    return max(sig, 0.03)


def _T_at(t):
    exp_dt = t.replace(hour=16, minute=0, second=0, microsecond=0)
    return max((exp_dt - t).total_seconds() / 60.0, 0.5) / MIN_PER_YEAR


# ── simulate one trade, returning a rich record ───────────────────────────────
def simulate(df, i, call, sig, rev, kchop, width):
    S0 = float(df['close'].iloc[i]); t0 = df.index[i]
    K = round(S0 / STRIKE_STEP) * STRIKE_STEP
    cost = position_value(S0, K, _T_at(t0), sig, width, call)
    if cost <= 0.01:
        return None, i
    grk = position_greeks(S0, K, _T_at(t0), sig, width, call)   # Greeks AT ENTRY
    mfe = mae = 0.0
    for j in range(i + 1, len(df)):
        t = df.index[j]; Sj = float(df['close'].iloc[j])
        val = position_value(Sj, K, _T_at(t), sig, width, call)
        ret = val / cost - 1.0
        mfe, mae = max(mfe, ret), min(mae, ret)
        reason = None
        if not A.no_tp and ret >= A.tp:                            reason = 'TP'
        elif ret <= -A.stop:                                       reason = 'STOP'
        elif (A.trail_trigger > 0 and mfe >= A.trail_trigger       # trailing stop, armed after peak
              and ret <= mfe * (1 - A.trail_giveback)):            reason = 'TRAIL'
        elif (call and rev[j] == -1) or (not call and rev[j] == 1): reason = 'REVERSE'
        elif t.hour == 15 and t.minute >= 55:                      reason = 'EOD'
        if reason:
            pnl = (val - cost) * MULT * QTY - FEE
            move_bp = (1 if call else -1) * (Sj - S0) / S0 * 1e4
            return {
                'day': t0.date().isoformat(), 'dow': t0.strftime('%a'),
                'quarter': f"{t0.year}Q{(t0.month-1)//3+1}",
                'entry': t0.strftime('%H:%M'), 'exit': t.strftime('%H:%M'),
                'hold_min': int((t - t0).total_seconds() // 60),
                'dir': 'CALL' if call else 'PUT', 'S_entry': round(S0, 2), 'strike': K,
                'width': width, 'kauf': round(float(kchop[i]), 1), 'iv': round(sig, 3),
                'delta': grk['delta'], 'gamma': grk['gamma'],
                'theta_$day': grk['theta_$day'], 'vega_$': grk['vega_$'],
                'cost_$': round(cost * MULT, 0), 'exit_$': round(val * MULT, 0),
                'pnl_$': round(pnl, 0), 'pnl_pct': round(pnl / (cost * MULT) * 100, 1),
                'outcome': 'win' if pnl > 0 else 'loss',
                'reason': reason, 'und_move_bp': round(move_bp, 1),
                'mfe_pct': round(mfe * 100), 'mae_pct': round(mae * 100),
            }, j
    return None, len(df) - 1


def collect(width):
    rows = []
    for day in DAYS:
        d = by_day[day]
        sdir = supertrend_dir(d); pdir = psar_dir(d); kchop = kaufman_chop(d); sig = day_iv(d)
        rev = pdir if A.reverse == 'psar' else sdir     # which trend's flip exits the trade
        open_until = -1
        for i in range(START, len(d)):
            if i <= open_until: continue
            if not (sdir[i] != 0 and sdir[i] != sdir[i-1]): continue   # trend forms
            if pdir[i] != sdir[i]: continue                            # PSAR agrees
            if not (kchop[i] <= A.kauf): continue                      # kaufman gate
            te = d.index[i].time()
            if WINDOWS is not None:                                     # entry time-window filter
                if not any(a <= te < b for a, b in WINDOWS): continue
            elif te >= CUTOFF: continue                                # or simple cutoff
            # entry-time realized vol — ONLY bars up to now (no lookahead), annualized
            lr = np.diff(np.log(d['close'].values[:i + 1]))
            ive = float(np.nanstd(lr) * sqrt(MIN_PER_YEAR)) if len(lr) > 2 else 0.0
            if A.skip_lowiv > 0 and ive < A.skip_lowiv:                # skip quiet-so-far days
                continue
            rec, exit_j = simulate(d, i, sdir[i] == 1, sig, rev, kchop, width)
            open_until = exit_j
            if rec:
                rec['iv_entry'] = round(ive, 3)
                rows.append(rec)
    return rows


def summarize(label, rows):
    n = len(rows); w = sum(r['outcome'] == 'win' for r in rows)
    g = sum(r['pnl_$'] for r in rows)
    cost = sum(r['cost_$'] for r in rows)
    aw = np.mean([r['pnl_$'] for r in rows if r['outcome'] == 'win']) if w else 0
    al = np.mean([r['pnl_$'] for r in rows if r['outcome'] == 'loss']) if (n - w) else 0
    roi = g / cost * 100 if cost else 0            # net $ ÷ total debit deployed
    mfe = np.mean([r['mfe_pct'] for r in rows]) if n else 0
    print(f"  {label:<10}{n:>4} tr | {w:>3}W/{n-w:<3}L ({w/n*100 if n else 0:>3.0f}%) | "
          f"net ${g:>+8.0f} | ROI {roi:>+5.1f}% | avgW ${aw:>+6.0f} | avgL ${al:>+6.0f} | avgMFE {mfe:>+4.0f}%")


def mfe_analysis(rows):
    """'Calc max profit for our understanding' — how high trades peaked, how much
    winners gave back riding to the reversal, and what a TP at each level would catch."""
    n = len(rows)
    if not n:
        return
    wins = [r for r in rows if r['outcome'] == 'win']
    print(f"  MAX-PROFIT (MFE) — no take-profit, so trades ride to reversal/stop/EOD:")
    if wins:
        peak = np.mean([r['mfe_pct'] for r in wins]); ex = np.mean([r['pnl_pct'] for r in wins])
        print(f"    winners: avg peak +{peak:.0f}% → exit +{ex:.0f}%  (gave back {peak-ex:.0f} pts riding the trend)")
    for thr in (40, 60, 80, 100, 150):
        hit = sum(1 for r in rows if r['mfe_pct'] >= thr)
        print(f"    reached +{thr:>3}% at some point: {hit:>3}/{n} ({hit/n*100:>3.0f}%)  — a TP there would have caught these")


# ── load bars ─────────────────────────────────────────────────────────────────
CACHE = A.cache or (CACHE_2Y if os.path.exists(CACHE_2Y) else CACHE_120)
if not os.path.exists(CACHE):
    raise SystemExit(f"No cache at {CACHE}. Run:  python scripts/pull_spx_2y.py")
df = pd.read_pickle(CACHE)
by_day = {d: g for d, g in df.groupby(df.index.date) if len(g) >= 60}
DAYS = sorted(by_day.keys())
START = 20
CUTOFF = pd.Timestamp('2000-01-01').replace(hour=int(A.cutoff), minute=int((A.cutoff % 1) * 60)).time()
WINDOWS = None
if A.windows:
    WINDOWS = [tuple(pd.Timestamp('2000-01-01 ' + x).time() for x in part.split('-'))
               for part in A.windows.split(',')]

_struct = 'single ATM leg' if A.legs == 'single' else 'ATM debit spread'
print(f"\nSPX 1-min from {os.path.basename(CACHE)}: {len(DAYS)} days {DAYS[0]}…{DAYS[-1]}  |  "
      f"1 contract {_struct}, fee ${FEE}/rt")
_when = f"windows {A.windows}" if A.windows else f"time<{A.cutoff:g}:00 ET"
_tp = 'NO-TP' if A.no_tp else f"tp+{A.tp:.0%}"
print(f"Entry: Supertrend flip + PSAR agree + kauf<={A.kauf:g} + {_when}   "
      f"Exit: {A.reverse}-reverse/stop-{A.stop:.0%}/{_tp}/EOD   IV=realized×{A.ivmult:g}")

for width in WIDTHS:
    rows = collect(width)
    hdr = _struct if A.legs == 'single' else f"${width:g}-wide spread"
    print(f"\n=== {hdr} — BY QUARTER ===")
    for q in sorted({r['quarter'] for r in rows}):
        summarize(q, [r for r in rows if r['quarter'] == q])
    print(f"  {'-'*72}")
    summarize('ALL', rows)
    mfe_analysis(rows)
    if len(WIDTHS) == 1:                       # single-width run → dump every trade
        pd.DataFrame(rows).to_csv(A.csv, index=False)
        print(f"\n  → wrote {len(rows)} trades to {A.csv}")

print("\nReal $ per 1 contract. mfe/mae = best/worst unrealized % during the trade.")
