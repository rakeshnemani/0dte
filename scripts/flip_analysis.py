"""'What if we flipped every signal?' — buy PUT when the logic says CALL and vice
versa, keeping ALL other logic identical. Honest empirical answer.

Method (same engine as replay_invalidation.py, so it's apples-to-apples):
  • take every real debit entry from audit.csv (06-30+),
  • replay it under the REAL exit rule (strategy.thesis_invalidated, live config),
  • once as-taken (NORMAL) and once with the direction flipped (FLIPPED),
  • measure the direction-adjusted underlying move in basis points, with the same
    TP/STOP proxies (TP=+40bp≈+60% spread, STOP=-55bp≈-70% spread).

Why not just negate the P&L? A losing bull-call-spread does NOT become a winning
bear-put-spread of equal size — both are long-premium debits, both pay the same
fees, and the invalidation exit fires on the OPPOSITE side when flipped (different
hold time). This re-simulates all of that. It's an underlying-move PROXY, not real
option P&L, and it EXCLUDES commissions (which are identical for both books).

READ-ONLY historical bars. clientId=14.
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
import config
import strategy

ET = pytz.timezone('America/New_York')
# Window start: optional CLI arg (YYYY-MM-DD), else the whole era.
ERA_START = sys.argv[1] if len(sys.argv) > 1 else '2026-06-30'
ET_CUTOVER = datetime.datetime(2026, 7, 5)
TP_BP, STOP_BP = 40, -55
FLIP = {'CALL': 'PUT', 'PUT': 'CALL'}

# Use the live production exit config explicitly.
config.VWAP_INVALIDATION_BARS = 6
config.VWAP_INVALIDATION_BUFFER_PCT = 0.0
config.VWAP_INVALIDATION_HOLD_ADX = 0.0

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

ib = IB()
try:
    ib.connect('127.0.0.1', 4002, clientId=14, timeout=20)
except Exception as e:
    raise SystemExit(f"IB Gateway not reachable: {e}")

bars_cache = {}
def day_bars(symbol, day):
    symbol = config.SIGNAL_SOURCE.get(symbol, symbol)   # XSP → SPY bars
    key = (symbol, day)
    if key in bars_cache:
        return bars_cache[key]
    end = day.replace('-', '') + ' 16:00:00 US/Eastern'
    raw = ib.reqHistoricalData(Stock(symbol, 'SMART', 'USD'), endDateTime=end,
                               durationStr='1 D', barSizeSetting='1 min',
                               whatToShow='TRADES', useRTH=True, formatDate=1, timeout=40)
    if not raw:
        bars_cache[key] = None
        return None
    df = util.df(raw).copy()
    df['date'] = pd.to_datetime(df['date'])
    df['date'] = (df['date'].dt.tz_localize('America/New_York') if df['date'].dt.tz is None
                  else df['date'].dt.tz_convert('America/New_York'))
    df = df.set_index('date')
    bars_cache[key] = df
    return df

def simulate(symbol, direction, ts, day):
    df = day_bars(symbol, day)
    if df is None:
        return None
    fwd = df[df.index >= ts]
    if fwd.empty:
        return None
    entry_px = float(fwd['close'].iloc[0])
    sign = 1 if direction == 'CALL' else -1
    for i in range(1, len(fwd)):
        t = fwd.index[i]
        move_bp = sign * (float(fwd['close'].iloc[i]) - entry_px) / entry_px * 1e4
        if move_bp >= TP_BP:
            return ('win', TP_BP)
        if move_bp <= STOP_BP:
            return ('loss', STOP_BP)
        if strategy.thesis_invalidated(direction, df[df.index <= t]):
            return ('win' if move_bp > 0 else 'loss', move_bp)
        if t.hour == 15 and t.minute >= 55:
            return ('win' if move_bp > 0 else 'loss', move_bp)
    last = sign * (float(fwd['close'].iloc[-1]) - entry_px) / entry_px * 1e4
    return ('win' if last > 0 else 'loss', last)

def run(flip):
    tot, wins, losses, no = 0.0, 0, 0, 0
    for e in entries:
        d = FLIP[e['direction']] if flip else e['direction']
        r = simulate(e['symbol'], d, e['ts'], e['day'])
        if r is None:
            no += 1
            continue
        outcome, bp = r
        tot += bp
        wins += outcome == 'win'
        losses += outcome == 'loss'
    return tot, wins, losses, no

print(f"\nReplaying {len(entries)} debit entries — NORMAL vs FLIPPED "
      f"(real exit rule, N=6). Underlying-move proxy, fees excluded.\n")
for label, flip in (("NORMAL (as-taken)", False), ("FLIPPED (CALL↔PUT)", True)):
    tot, wins, losses, no = run(flip)
    wr = wins / (wins + losses) * 100 if (wins + losses) else 0
    print(f"{label:<20} total {tot:>+6.0f} bp | {wins}W/{losses}L ({wr:.0f}% win) | {no} no-data")

print("\nProxies: win-cap +40bp (~+60% spread), loss-cap -55bp (~-70%).")
print("NB: commissions (~$380 this era, identical for both books) are NOT in these")
print("numbers — a positive-bp flipped book can still be net-negative after fees.")
ib.disconnect()
