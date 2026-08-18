"""Backfill the PermId column in audit.csv from IBKR executions.

Only recent rows are recoverable — reqExecutions covers ~the last 24h. Older
rows (before permId logging existed) stay blank; they'd need an IBKR Flex Query
(TODO #24). Read-only against IBKR. Backs up audit.csv before rewriting, and only
fills EMPTY PermId cells (never overwrites).

    python scripts/backfill_permid.py

Matching: an audit row is assigned a permId when exactly ONE IBKR order matches
by (symbol, net price within $0.02, execution time within 10 min of the row).
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import csv
import datetime
import os
import shutil

import pytz
from ib_insync import IB, ExecutionFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT = os.path.join(ROOT, 'audit.csv')
ET = pytz.timezone('America/New_York')  # audit timestamps are ET — match in ET

ib = IB()
try:
    ib.connect('127.0.0.1', 4002, clientId=11, timeout=20)
except Exception as e:
    raise SystemExit(f"Could not connect to IB Gateway: {e}")

ib.reqExecutions(ExecutionFilter())
ib.sleep(2)

# One entry per permId: symbol, fill price, and execution time (ET, naive).
orders = {}
for f in ib.fills():
    e, c = f.execution, f.contract
    o = orders.setdefault(e.permId, {'symbol': c.symbol, 'price': None,
                                     'time': f.time.astimezone(ET).replace(tzinfo=None)})
    if c.secType in ('BAG', 'OPT'):   # net price on a BAG combo, or the single OPT fill
        o['price'] = e.price
ib.disconnect()

orders = {k: v for k, v in orders.items() if v['price'] is not None}
print(f"Pulled {len(orders)} IBKR orders with a fill price.")


def find_permid(symbol, price, ts):
    hits = []
    for permid, o in orders.items():
        if o['symbol'] != symbol:
            continue
        if abs(o['price'] - price) > 0.02:
            continue
        if abs((o['time'] - ts).total_seconds()) > 600:
            continue
        hits.append(permid)
    return hits[0] if len(hits) == 1 else None


with open(AUDIT) as fh:
    rows = list(csv.reader(fh))
header, data = rows[0], rows[1:]
idx = {name: i for i, name in enumerate(header)}
pi, ti, si, pri, ai = idx['PermId'], idx['Timestamp'], idx['Symbol'], idx['Price'], idx['Action']

filled, skipped = 0, 0
for r in data:
    if len(r) > pi and (r[pi] or '').strip():
        continue  # already has a permId — never overwrite
    if r[ai] not in ('BUY', 'SELL') or not (r[pri] or '').strip():
        continue
    try:
        ts = datetime.datetime.strptime(r[ti], '%Y-%m-%d %H:%M:%S')
        price = round(float(r[pri]), 2)
    except (ValueError, IndexError):
        continue
    permid = find_permid(r[si], price, ts)
    if permid:
        while len(r) <= pi:
            r.append('')
        r[pi] = str(permid)
        filled += 1
        print(f"  matched {r[ti]} {r[si]:4} {r[ai]:4} @ {price} → permId {permid}")
    else:
        skipped += 1

shutil.copy(AUDIT, AUDIT + '.bak')
with open(AUDIT, 'w', newline='') as fh:
    w = csv.writer(fh)
    w.writerow(header)
    w.writerows(data)

print(f"\nBackfilled {filled} rows, {skipped} unmatched (older than the API window or ambiguous).")
print(f"Backup saved to {AUDIT}.bak")
