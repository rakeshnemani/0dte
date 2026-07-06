"""Counterfactual checker: what did a symbol do after a given ET time today?

Used for retro work — e.g. when the chop guard blocks an entry, run this to see
whether the blocked trade would have won (guard cost us) or lost (guard saved us).

Usage (with the bot running or not — uses its own clientId):
    python scripts/counterfactual.py IWM 10:07
"""
import sys
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, Stock, util
import pandas as pd

symbol = sys.argv[1] if len(sys.argv) > 1 else 'IWM'
after = sys.argv[2] if len(sys.argv) > 2 else '10:00'
after_h, after_m = (int(x) for x in after.split(':'))

ib = IB()
ib.connect('127.0.0.1', 4002, clientId=99, timeout=15)
ib.reqMarketDataType(4)

c = Stock(symbol, 'SMART', 'USD')
ib.qualifyContracts(c)
bars = ib.reqHistoricalData(c, endDateTime='', durationStr='1 D',
                            barSizeSetting='5 mins', whatToShow='TRADES',
                            useRTH=True, formatDate=1, timeout=30)
ib.disconnect()

df = util.df(bars)
df['date'] = pd.to_datetime(df['date'])
if df['date'].dt.tz is None:
    df['date'] = df['date'].dt.tz_localize('America/New_York')
else:
    df['date'] = df['date'].dt.tz_convert('America/New_York')
df = df.set_index('date')

session_open = df.index[-1].replace(hour=9, minute=30, second=0, microsecond=0)
today = df[df.index >= session_open]
cutoff = df.index[-1].replace(hour=after_h, minute=after_m, second=0, microsecond=0)
before, after_df = today[today.index < cutoff], today[today.index >= cutoff]

print(f"\n{symbol} today ({session_open.date()}), 5-min bars — session so far:")
print(today[['open', 'high', 'low', 'close']].to_string())

if not after_df.empty:
    ref = after_df['open'].iloc[0]
    hi, lo, last = after_df['high'].max(), after_df['low'].min(), after_df['close'].iloc[-1]
    print(f"\nSince {after} ET (ref {ref:.2f}):")
    print(f"  High : {hi:.2f}  ({(hi/ref-1)*100:+.2f}%)")
    print(f"  Low  : {lo:.2f}  ({(lo/ref-1)*100:+.2f}%)")
    print(f"  Last : {last:.2f}  ({(last/ref-1)*100:+.2f}%)")
    print("\nRough verdict for a blocked CALL entry: sustained move above ref = guard cost us;")
    print("flat/lower = guard saved us. (Spread P&L amplifies these small % moves ~30-50x.)")
