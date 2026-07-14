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
sys.path.insert(0, os.path.join(ROOT, 'src'))
import config       # noqa: E402  — the real bot config (knobs mutated per scenario)
import strategy     # noqa: E402  — replay uses the REAL thesis_invalidated rule
ET = pytz.timezone('America/New_York')
ERA_START = '2026-06-30'
ET_CUTOVER = datetime.datetime(2026, 7, 5)   # audit rows before this are CDT (+1h -> ET)

TP_BP = 40      # favorable underlying move ≈ +60% spread (proxy)
STOP_BP = -55   # adverse underlying move ≈ -70% spread (proxy)

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


# ── bars from IBKR, one fetch per (symbol, day) ──────────────────────────────
ib = IB()
try:
    ib.connect('127.0.0.1', 4002, clientId=12, timeout=20)
except Exception as e:
    raise SystemExit(f"IB Gateway not reachable: {e}")

bars_cache = {}
def day_bars(symbol, day):
    # Faithful to production: indicators come from the signal source, so XSP
    # entries replay on SPY bars (real volume for VWAP; XSP itself is an index
    # with no TRADES volume and would fail a Stock() fetch). SPY/QQQ/IWM map to
    # themselves. Caching on the resolved symbol also de-dups XSP↔SPY fetches.
    symbol = config.SIGNAL_SOURCE.get(symbol, symbol)
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

def simulate(e, n, buffer=0.0, hold_adx=0.0):
    """Walk forward from entry; exit on the TP/STOP proxy or the REAL bot
    invalidation rule (strategy.thesis_invalidated) under this scenario's #32
    knobs. Faithful to production: the rule sees the full session bars up to the
    current minute and uses ta rolling-VWAP (+ ADX for the hold). (label, bp, m)."""
    df = day_bars(e['symbol'], e['day'])
    if df is None:
        return None
    fwd = df[df.index >= e['ts']]
    if fwd.empty:
        return None
    entry_px = float(fwd['close'].iloc[0])
    sign = 1 if e['direction'] == 'CALL' else -1
    config.VWAP_INVALIDATION_BARS = n
    config.VWAP_INVALIDATION_BUFFER_PCT = buffer
    config.VWAP_INVALIDATION_HOLD_ADX = hold_adx
    for i in range(1, len(fwd)):
        t = fwd.index[i]
        mins = (t - e['ts']).total_seconds() / 60
        move_bp = sign * (float(fwd['close'].iloc[i]) - entry_px) / entry_px * 1e4
        if move_bp >= TP_BP:
            return ('TP-likely', TP_BP, mins)
        if move_bp <= STOP_BP:
            return ('HARD-STOP', STOP_BP, mins)
        if strategy.thesis_invalidated(e['direction'], df[df.index <= t]):
            return ('invalidated', move_bp, mins)
        if t.hour == 15 and t.minute >= 55:
            return ('EOD', move_bp, mins)
    last_bp = sign * (float(fwd['close'].iloc[-1]) - entry_px) / entry_px * 1e4
    return ('EOD', last_bp, (fwd.index[-1] - e['ts']).total_seconds() / 60)


# ── scenarios: current rule vs #32 variants (same bp metric as the N sweep) ──
SCENARIOS = [
    ("N=6 current",             dict(n=6)),
    ("N=6 +buf0.05%",           dict(n=6, buffer=0.0005)),
    ("N=6 +buf0.10%",           dict(n=6, buffer=0.0010)),
    ("N=6 +holdADX35",          dict(n=6, hold_adx=35.0)),
    ("N=6 +holdADX40",          dict(n=6, hold_adx=40.0)),
    ("N=6 +buf0.05%+holdADX35", dict(n=6, buffer=0.0005, hold_adx=35.0)),
]

print(f"\nReplaying {len(entries)} entries × {len(SCENARIOS)} scenarios "
      f"(real thesis_invalidated rule)...\n")

results = {name: {'bp': 0.0, 'labels': {}} for name, _ in SCENARIOS}
for e in entries:
    for name, kw in SCENARIOS:
        r = simulate(e, **kw)
        if r is None:
            continue
        results[name]['bp'] += r[1]
        results[name]['labels'][r[0]] = results[name]['labels'].get(r[0], 0) + 1

print(f"{'scenario':<26}{'total bp':>9}   outcome mix")
print("-" * 68)
for name, _ in SCENARIOS:
    R = results[name]
    mix = ' '.join(f"{k}:{v}" for k, v in sorted(R['labels'].items()))
    print(f"{name:<26}{R['bp']:>+9.0f}   {mix}")

print("\nProxies: TP-likely=+40bp (~+60% spread); HARD-STOP=-55bp (~-70%).")
print("Higher total bp = that rule left trades in better spots (same metric as the N sweep).")
print("Revert-trigger discipline still applies before enabling any knob live.")
ib.disconnect()
