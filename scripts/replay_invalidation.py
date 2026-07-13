"""Replay historical entries under different VWAP_INVALIDATION_BARS settings.

    python scripts/replay_invalidation.py 3 6      # compare N=3 vs N=6 (default)

For every debit BUY in audit.csv (current era, 06-30+), fetches that day's 1-min
bars from IBKR, recomputes VWAP, and simulates the invalidation exit under each N:
walk forward from entry, count consecutive closes on the wrong side of VWAP, exit
when the count hits N (or 15:55 EOD flatten).

Outcomes are measured on the UNDERLYING (we can't reprice the options
historically). Direction-adjusted move in basis points, with rough spread-P&L
proxies, clearly labeled as proxies:
    favorable move >= +0.40%  ->  "TP-likely"   (~+60% on an ATM $1 vertical)
    adverse  move  <= -0.55%  ->  "HARD-STOP"   (~-70%)

READ-ONLY against IBKR (historical bars only). clientId=12 — safe alongside the bot.
Note: IBKR 1-min history covers our whole era; each (symbol, day) is fetched once.
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import csv
import datetime
import os
import sys

import pandas as pd
import pytz
from ib_insync import IB, Stock, util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = pytz.timezone('America/New_York')
ERA_START = '2026-06-30'
ET_CUTOVER = datetime.datetime(2026, 7, 5)   # audit rows before this are CDT (+1h -> ET)

TP_BP = 40      # favorable underlying move ≈ +60% spread (proxy)
STOP_BP = -55   # adverse underlying move ≈ -70% spread (proxy)

Ns = [int(a) for a in sys.argv[1:]] or [3, 6]

# ── entries from the audit ────────────────────────────────────────────────────
entries = []
with open(os.path.join(ROOT, 'audit.csv')) as fh:
    for r in csv.DictReader(fh):
        if r['Action'] != 'BUY' or r['Direction'] not in ('CALL', 'PUT'):
            continue
        if r['Timestamp'] < ERA_START:
            continue
        ts = datetime.datetime.strptime(r['Timestamp'], '%Y-%m-%d %H:%M:%S')
        if ts < ET_CUTOVER:
            ts += datetime.timedelta(hours=1)
        entries.append({'symbol': r['Symbol'], 'direction': r['Direction'],
                        'ts': ET.localize(ts), 'day': ts.date().isoformat()})

print(f"Replaying {len(entries)} debit entries under N={Ns} ...")

# ── bars from IBKR, one fetch per (symbol, day) ──────────────────────────────
ib = IB()
try:
    ib.connect('127.0.0.1', 4002, clientId=12, timeout=20)
except Exception as e:
    raise SystemExit(f"IB Gateway not reachable: {e}")

bars_cache = {}
def day_bars(symbol, day):
    key = (symbol, day)
    if key in bars_cache:
        return bars_cache[key]
    end = day.replace('-', '') + ' 16:00:00 US/Eastern'
    raw = ib.reqHistoricalData(Stock(symbol, 'SMART', 'USD'), endDateTime=end,
                               durationStr='1 D', barSizeSetting='1 min',
                               whatToShow='TRADES', useRTH=True, formatDate=1,
                               timeout=40)
    if not raw:
        bars_cache[key] = None
        return None
    df = util.df(raw).copy()
    df['date'] = pd.to_datetime(df['date'])
    df['date'] = (df['date'].dt.tz_localize('America/New_York') if df['date'].dt.tz is None
                  else df['date'].dt.tz_convert('America/New_York'))
    df = df.set_index('date')
    tp = (df['high'] + df['low'] + df['close']) / 3
    df['VWAP'] = (tp * df['volume']).cumsum() / df['volume'].cumsum().replace(0, 1)
    bars_cache[key] = df
    return df

def simulate(e, n):
    """Exit under N consecutive wrong-side closes. Returns (label, bp, minutes)."""
    df = day_bars(e['symbol'], e['day'])
    if df is None:
        return None
    fwd = df[df.index >= e['ts']]
    if fwd.empty:
        return None
    entry_px = float(fwd['close'].iloc[0])
    sign = 1 if e['direction'] == 'CALL' else -1
    streak, best = 0, 0.0
    for i in range(1, len(fwd)):
        bar = fwd.iloc[i]
        t = fwd.index[i]
        move_bp = sign * (float(bar['close']) - entry_px) / entry_px * 1e4
        best = max(best, move_bp)
        if move_bp >= TP_BP:
            return ('TP-likely', TP_BP, (t - e['ts']).total_seconds() / 60)
        if move_bp <= STOP_BP:
            return ('HARD-STOP', STOP_BP, (t - e['ts']).total_seconds() / 60)
        wrong = (float(bar['close']) < float(bar['VWAP'])) if sign == 1 \
            else (float(bar['close']) > float(bar['VWAP']))
        streak = streak + 1 if wrong else 0
        if streak >= n:
            return ('invalidated', move_bp, (t - e['ts']).total_seconds() / 60)
        if t.hour == 15 and t.minute >= 55:
            return ('EOD', move_bp, (t - e['ts']).total_seconds() / 60)
    last_bp = sign * (float(fwd['close'].iloc[-1]) - entry_px) / entry_px * 1e4
    return ('EOD', last_bp, (fwd.index[-1] - e['ts']).total_seconds() / 60)

# ── run + report ──────────────────────────────────────────────────────────────
totals = {n: 0.0 for n in Ns}
print(f"\n{'entry':<22}{'dir':<5}", *[f"N={n}: outcome  bp   held".ljust(30) for n in Ns])
for e in entries:
    cells = []
    for n in Ns:
        r = simulate(e, n)
        if r is None:
            cells.append('no data'.ljust(30))
            continue
        label, bp, mins = r
        totals[n] += bp
        cells.append(f"{label:<12}{bp:+6.0f}  {mins:4.0f}m".ljust(30))
    print(f"{e['day']} {e['ts'].strftime('%H:%M')}  {e['symbol']:<5}{e['direction']:<5}", *cells)

print("\n--- direction-adjusted underlying totals (bp; TP/STOP capped at proxies) ---")
for n in Ns:
    print(f"  N={n}: {totals[n]:+.0f} bp across {len(entries)} entries")
print("\nProxies: TP-likely = +40bp favorable (~+60% spread); HARD-STOP = -55bp (~-70%).")
print("Interpretation: higher total bp = that N setting left trades in better spots.")
ib.disconnect()
