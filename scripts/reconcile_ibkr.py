"""Reconcile IBKR trades against audit.csv — keyed on permId.

READ-ONLY against IBKR (no orders placed/cancelled). Answers: "did the bot's
books match what actually happened in the account, on any given day?"

    python scripts/reconcile_ibkr.py                  # today (live API)
    python scripts/reconcile_ibkr.py 2026-07-09       # a recent day (live API, ~24h window)
    python scripts/reconcile_ibkr.py 2026-06-15       # older day -> auto-uses Flex Query if configured
    python scripts/reconcile_ibkr.py 2026-07-09 --write   # append orphan flags to audit.csv

Data sources
------------
• Live API (reqExecutions + reqPnL): today / last ~24h. Also gives account dailyPnL
  (the one number that includes expiry/exercise settlement).
• Flex Query: any date up to ~1 year — IF the two env vars below are set. Create a
  "Trade Confirmation" Flex Query in IBKR Client Portal → Settings → Flex Queries,
  then a token under Reporting → Flex Web Service:
      IBKR_FLEX_TOKEN=...    IBKR_FLEX_QUERY_ID=...

permId is the join key: every audit BUY row carries its entry order's permId and
every SELL its exit order's permId, so IBKR orders match audit rows exactly.
An IBKR order with NO matching audit row is an ORPHAN — a fill the bot didn't book.
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import collections
import csv
import datetime
import os
import shutil
import sys

import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT = os.path.join(ROOT, 'audit.csv')
ET = pytz.timezone('America/New_York')
SENTINEL = 1.0e18   # ib_insync's "realizedPNL unset" is ~1.8e308

args = [a for a in sys.argv[1:] if not a.startswith('--')]
flags = {a for a in sys.argv[1:] if a.startswith('--')}
day = args[0] if args else datetime.date.today().isoformat()
WRITE = '--write' in flags


# ── IBKR order summaries (permId -> dict), from whichever source fits the date ──

def orders_from_live():
    """Recent executions via the live API. Returns (orders, account_pnl, open_opts)."""
    from ib_insync import IB, ExecutionFilter
    ib = IB()
    try:
        ib.connect('127.0.0.1', 4002, clientId=9, timeout=20)
    except Exception as e:
        print(f"Could not connect to IB Gateway (127.0.0.1:4002): {type(e).__name__}: {e}")
        print("Start IB Gateway and log in, then re-run. (The bot can stay running — this uses clientId 9.)")
        raise SystemExit(1)

    acct_pnl = None
    try:
        acct_pnl = ib.reqPnL(ib.managedAccounts()[0]); ib.sleep(2)
    except Exception:
        pass

    ib.reqExecutions(ExecutionFilter()); ib.sleep(2)
    orders = {}
    for f in ib.fills():
        if f.time.astimezone(ET).date().isoformat() != day:
            continue
        e, c, cr = f.execution, f.contract, f.commissionReport
        o = orders.setdefault(e.permId, {
            'symbol': c.symbol, 'time': f.time.astimezone(ET).strftime('%H:%M:%S'),
            'side': None, 'price': None, 'realized': 0.0, 'commission': 0.0})
        if c.secType == 'BAG':          # the BAG line carries side + net price
            o['side'], o['price'] = e.side, e.price
        if cr:
            if cr.realizedPNL is not None and abs(cr.realizedPNL) < SENTINEL:
                o['realized'] += cr.realizedPNL
            o['commission'] += cr.commission or 0.0
    open_opts = [p for p in ib.positions()
                 if p.contract.secType == 'OPT' and p.position != 0]
    ib.disconnect()
    return orders, acct_pnl, open_opts


def orders_from_flex():
    """Historical trades via an IBKR Flex Query. None if not configured/usable."""
    token, qid = os.getenv('IBKR_FLEX_TOKEN'), os.getenv('IBKR_FLEX_QUERY_ID')
    if not (token and qid):
        return None
    try:
        from ib_insync import FlexReport
        rep = FlexReport(token, qid)
        df = rep.df('TradeConfirm')
        if df is None or df.empty:
            df = rep.df('Trade')
    except Exception as e:
        print(f"Flex Query failed: {e}")
        return None

    orders = {}
    daycmp = day.replace('-', '')
    for _, r in df.iterrows():
        td = str(r.get('tradeDate', ''))
        if td and td != daycmp:
            continue
        permid = int(r.get('permId') or r.get('ibOrderID') or 0)
        if not permid:
            continue
        o = orders.setdefault(permid, {'symbol': r.get('underlyingSymbol') or r.get('symbol'),
                                       'time': str(r.get('tradeTime', '')), 'side': r.get('buySell'),
                                       'price': None, 'realized': 0.0, 'commission': 0.0})
        o['realized'] += float(r.get('fifoPnlRealized') or 0.0)
        o['commission'] += float(r.get('ibCommission') or 0.0)
    return orders


# ── audit.csv for the day ─────────────────────────────────────────────────────

def read_audit():
    if not os.path.isfile(AUDIT):
        return [], {}, 0.0, 0.0
    with open(AUDIT) as fh:
        reader = csv.DictReader(fh)
        rows = [r for r in reader if (r['Timestamp'] or '').startswith(day)
                and r['Action'] in ('BUY', 'SELL')]
    perm_to_row = {}
    gross = fees = 0.0
    for r in rows:
        pid = (r.get('PermId') or '').strip()
        if pid:
            perm_to_row[pid] = r
        if r['Action'] == 'SELL' and (r['Dollar_PnL'] or '').strip():
            gross += float(r['Dollar_PnL'])
        c = (r.get('Commission') or '').strip()
        if c:
            fees += float(c)
    return rows, perm_to_row, gross, fees


# ── report (pure — testable offline with mock orders) ────────────────────────

def report(orders, acct_pnl, open_opts):
    orders = {k: v for k, v in orders.items() if v.get('symbol')}
    rows, perm_to_row, audit_gross, audit_fees = read_audit()
    audit_permids = set(perm_to_row)

    print(f"\n=== permId reconciliation for {day} ===")
    print(f"IBKR orders: {len(orders)}   |   audit rows with permId: {len(audit_permids)}\n")

    ibkr_realized = ibkr_comm = 0.0
    matched = orphans = 0
    print(f"{'permId':>12}  {'time':>8}  {'sym':<4} {'side':<4} {'price':>6}  {'realized':>9}  {'comm':>6}  audit")
    for permid, o in sorted(orders.items(), key=lambda kv: kv[1]['time']):
        ibkr_realized += o['realized']
        ibkr_comm += o['commission']
        in_audit = str(permid) in audit_permids
        matched += in_audit
        orphans += (not in_audit)
        tag = "OK" if in_audit else "<-- ORPHAN (not booked)"
        print(f"{permid:>12}  {o['time']:>8}  {o['symbol']:<4} {str(o['side'] or ''):<4} "
              f"{(o['price'] if o['price'] is not None else 0):>6.2f}  {o['realized']:>+9.2f}  "
              f"{o['commission']:>6.2f}  {tag}")

    # audit-internal orphan check: BUYs with no matching SELL for the day (the
    # 2026-07-09 signature — a position opened but never closed in the books)
    opens = collections.Counter(r['Symbol'] for r in rows if r['Action'] == 'BUY')
    closes = collections.Counter(r['Symbol'] for r in rows if r['Action'] == 'SELL')
    unclosed = {s: opens[s] - closes[s] for s in opens if opens[s] > closes[s]}

    print(f"\n--- summary ---")
    print(f"Matched IBKR orders: {matched}   |   ORPHAN IBKR orders (no audit row): {orphans}")
    if unclosed:
        print("!!  Audit BUYs with no SELL today: " +
              ", ".join(f"{s}x{n}" for s, n in unclosed.items()) + "  (opened, never booked closed)")
    print(f"IBKR realized P&L: ${ibkr_realized:+.2f}   commissions: ${ibkr_comm:.2f}")
    if acct_pnl is not None:
        print(f"Account dailyPnL (incl. expiry settlement): ${acct_pnl.dailyPnL:+.2f}  <-- true day total")
    print(f"Audit booked: gross ${audit_gross:+.2f}   fees ${audit_fees:.2f}   net ${audit_gross - audit_fees:+.2f}")

    if open_opts:
        print(f"\n=== Still-open option positions ({len(open_opts)}) ===")
        for p in open_opts:
            c = p.contract
            print(f"   {c.symbol} {c.right}{c.strike:g} exp {c.lastTradeDateOrContractMonth} pos {p.position:+g}")

    if WRITE and orphans:
        _append_orphans(orders, audit_permids)
        print(f"\nAppended {orphans} orphan RECONCILE row(s) to audit.csv (backup: audit.csv.bak).")
    elif WRITE:
        print("\n--write: nothing to append (no orphan IBKR orders).")


def _append_orphans(orders, audit_permids):
    shutil.copy(AUDIT, AUDIT + '.bak')
    with open(AUDIT) as fh:
        header = next(csv.reader(fh))
    pi = {n: i for i, n in enumerate(header)}
    with open(AUDIT, 'a', newline='') as fh:
        w = csv.writer(fh)
        for permid, o in orders.items():
            if str(permid) in audit_permids:
                continue
            row = [''] * len(header)
            row[pi['Timestamp']] = f"{day} 16:20:00"
            row[pi['Action']] = 'RECONCILE'
            row[pi['Symbol']] = o['symbol']
            row[pi['Direction']] = 'ORPHAN'
            row[pi['Reason']] = (f"IBKR order permId {permid} not in audit (orphan/untracked). "
                                 f"side {o['side']} @ {o['price']}. Verify against statement.")
            row[pi['Dollar_PnL']] = f"{o['realized']:.2f}"
            row[pi['Commission']] = f"{o['commission']:.2f}"
            row[pi['PermId']] = str(permid)
            w.writerow(row)


def main():
    is_recent = (datetime.date.today() - datetime.date.fromisoformat(day)).days <= 1
    if is_recent:
        orders, acct_pnl, open_opts = orders_from_live()
    else:
        orders = orders_from_flex()
        if orders is None:
            print(f"{day} is outside the live-API window and no Flex Query is configured.")
            print("Set IBKR_FLEX_TOKEN + IBKR_FLEX_QUERY_ID (see docstring) for historical dates.")
            raise SystemExit(1)
        acct_pnl, open_opts = None, []
    report(orders, acct_pnl, open_opts)


if __name__ == '__main__':
    main()
