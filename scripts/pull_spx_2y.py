"""One-time: pull ~2 years of SPX 1-min RTH bars → scripts/.spx_1min_2y_cache.pkl.

Chunked + paced backward walk. Safe to re-run: it extends/rebuilds the cache. Needs the
IB Gateway data line free — if you see error 162 'session connected from a different IP',
log out of other IBKR sessions (mobile app / web Client Portal / another TWS) or restart
the Gateway, then re-run. clientId=18.

  python scripts/pull_spx_2y.py            # ~2 years
  python scripts/pull_spx_2y.py 3          # ~3 years
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import os, sys
import pandas as pd
from ib_insync import IB, Index, util

YEARS = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
CHUNKS = int(YEARS * 365 / 20) + 2                 # ~20 calendar days per request
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.spx_1min_2y_cache.pkl')

ib = IB()
try:
    ib.connect('127.0.0.1', 4002, clientId=18, timeout=20)
except Exception as e:
    raise SystemExit(f"IB Gateway not reachable: {e}")
spx = Index('SPX', 'CBOE', 'USD'); ib.qualifyContracts(spx)


def pull(what):
    seen, end = {}, ''
    for k in range(CHUNKS):
        try:
            raw = ib.reqHistoricalData(spx, endDateTime=end, durationStr='20 D',
                                       barSizeSetting='1 min', whatToShow=what,
                                       useRTH=True, formatDate=1, timeout=90)
        except Exception as e:
            print(f"  chunk {k}: error {e} — sleeping 60s, retrying once"); ib.sleep(60)
            raw = ib.reqHistoricalData(spx, endDateTime=end, durationStr='20 D',
                                       barSizeSetting='1 min', whatToShow=what,
                                       useRTH=True, formatDate=1, timeout=90)
        if not raw:
            print(f"  chunk {k}: empty (reached data limit or feed blocked) — stopping"); break
        for b in raw:
            seen[b.date] = b
        oldest = raw[0].date
        print(f"  chunk {k+1}/{CHUNKS}: +{len(raw)} bars, back to {oldest}  (total {len(seen)})")
        end = oldest
        ib.sleep(2)                                # pacing: stay well under 60 req / 10 min
    return seen


print(f"Pulling ~{YEARS:g}y of SPX 1-min RTH ({CHUNKS} chunks of 20D)…")
seen = pull('TRADES')
if not seen:
    print("TRADES empty — trying MIDPOINT…")
    seen = pull('MIDPOINT')
ib.disconnect()
if not seen:
    raise SystemExit("No bars returned. Free the Gateway data line (error 162) and re-run.")

df = util.df(sorted(seen.values(), key=lambda b: b.date)).copy()
df['date'] = pd.to_datetime(df['date'])
df['date'] = (df['date'].dt.tz_localize('America/New_York') if df['date'].dt.tz is None
              else df['date'].dt.tz_convert('America/New_York'))
df = df.set_index('date')
df.to_pickle(OUT)
print(f"\nSaved {len(df)} bars → {OUT}  ({df.index[0].date()} … {df.index[-1].date()})")
