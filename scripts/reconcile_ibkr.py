"""Pull executions from IBKR and reconcile against audit.csv.

READ-ONLY against IBKR (reqExecutions) — places no orders, cancels nothing.
Serves two needs:
  • "get today's trades from IBKR and compare"        (run with no args)
  • ad-hoc "pull trade P&L by date and compare"        (run with a YYYY-MM-DD)

    python scripts/reconcile_ibkr.py               # today
    python scripts/reconcile_ibkr.py 2026-07-09    # a specific recent day

Limitation: the live API (reqExecutions) only returns roughly the last 24h of
executions. For older dates use an IBKR Flex Query (web) — see TODO #24.
Also: options that EXPIRE (worthless or auto-exercised) are settlement events,
not executions, so they will NOT appear here — those must be read from the
account statement / P&L. Orphaned positions that expired are the main such case.
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import collections
import csv
import datetime
import os
import sys

from ib_insync import IB, ExecutionFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT = os.path.join(ROOT, 'audit.csv')
SENTINEL = 1.0e18   # ib_insync's "realizedPNL unset" is ~1.8e308

day = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()

ib = IB()
try:
    ib.connect('127.0.0.1', 4002, clientId=9, timeout=20)
except Exception as e:
    print(f"Could not connect to IB Gateway (127.0.0.1:4002): {e}")
    print("Is IB Gateway running and logged in? (The bot can keep running — this uses clientId 9.)")
    sys.exit(1)

# Account-level P&L — captures expiry/exercise settlement that executions miss
acct_pnl = None
try:
    acct = ib.managedAccounts()[0]
    acct_pnl = ib.reqPnL(acct)
    ib.sleep(2)
except Exception as e:
    print(f"(account P&L unavailable: {e})")

# Pull all of today's executions (empty filter = every client, this account)
ib.reqExecutions(ExecutionFilter())
ib.sleep(2)
fills = [f for f in ib.fills() if str(f.time.date()) == day or f.time.date().isoformat() == day]

print(f"\n=== IBKR executions for {day} ===")
if not fills:
    print("No executions returned (either none today, or the day is outside the ~24h API window).")

# Group by permId — the order-level primary key. Each combo leg is one execution.
by_perm = collections.defaultdict(list)
for f in fills:
    by_perm[f.execution.permId].append(f)

ibkr_realized = 0.0
ibkr_commissions = 0.0
for permid, group in sorted(by_perm.items(), key=lambda kv: kv[1][0].time):
    g0 = group[0]
    t = g0.time.astimezone().strftime('%H:%M:%S')
    sym = g0.contract.symbol
    print(f"\npermId {permid}  {t}  {sym}")
    for f in group:
        c, e, cr = f.contract, f.execution, f.commissionReport
        leg = (f"{c.right}{c.strike:g}" if c.secType == 'OPT' else c.secType)
        rpnl = cr.realizedPNL if cr and abs(cr.realizedPNL) < SENTINEL else None
        comm = cr.commission if cr else 0.0
        ibkr_commissions += comm or 0.0
        if rpnl is not None:
            ibkr_realized += rpnl
        print(f"   {e.side:3} {e.shares:>3g} {leg:<7} @ {e.price:<7g} "
              f"comm ${comm or 0:>6.2f}" + (f"  realizedPNL ${rpnl:+.2f}" if rpnl is not None else ""))

print(f"\n--- IBKR totals for {day} ---")
print(f"Realized P&L (from closing fills): ${ibkr_realized:+.2f}")
print(f"Commissions (all fills):           ${ibkr_commissions:.2f}")
print(f"Net (realized − commissions):      ${ibkr_realized - ibkr_commissions:+.2f}")
if acct_pnl is not None:
    print(f"\n--- Account-level P&L (includes expiry/exercise settlement) ---")
    print(f"Account dailyPnL:      ${acct_pnl.dailyPnL:+.2f}   <-- true total for the day")
    print(f"Account realizedPnL:   ${acct_pnl.realizedPnL:+.2f}")
    print(f"Account unrealizedPnL: ${acct_pnl.unrealizedPnL:+.2f}")

# Currently-open option positions (the orphans, if the market is still open)
open_opts = [p for p in ib.positions() if p.contract.secType == 'OPT' and p.position != 0]
if open_opts:
    print(f"\n=== Still-open option positions (account holds these NOW) ===")
    for p in open_opts:
        c = p.contract
        print(f"   {c.symbol} {c.right}{c.strike:g} exp {c.lastTradeDateOrContractMonth} "
              f"pos {p.position:+g}  avgCost ${p.avgCost:.2f}")
else:
    print("\n(No open option positions right now — all settled/closed.)")

# Per-orphan settlement P&L, grouped by symbol (for booking into the audit)
if open_opts and acct_pnl is not None:
    print("\n=== Orphaned-trade settlement P&L (per symbol, via reqPnLSingle) ===")
    by_sym = collections.defaultdict(lambda: [0.0, 0.0])
    acct = ib.managedAccounts()[0]
    for p in open_opts:
        try:
            s = ib.reqPnLSingle(acct, '', p.contract.conId)
            ib.sleep(1.5)
            u = s.unrealizedPnL if s.unrealizedPnL is not None and abs(s.unrealizedPnL) < SENTINEL else 0.0
            r = s.realizedPnL if s.realizedPnL is not None and abs(s.realizedPnL) < SENTINEL else 0.0
            by_sym[p.contract.symbol][0] += u
            by_sym[p.contract.symbol][1] += r
            ib.cancelPnLSingle(acct, '', p.contract.conId)
        except Exception as e:
            print(f"  {p.contract.symbol} conId {p.contract.conId}: PnLSingle failed: {e}")
    for sym, (u, r) in sorted(by_sym.items()):
        print(f"  {sym}: unrealized ${u:+.2f}  realized ${r:+.2f}  → settles to ${u + r:+.2f}")

# Compare to our audit.csv for the same day
print(f"\n=== audit.csv for {day} ===")
audit_pnl, audit_fees, sells = 0.0, 0.0, 0
if os.path.isfile(AUDIT):
    with open(AUDIT) as fh:
        for r in csv.DictReader(fh):
            if not r['Timestamp'].startswith(day) or r['Action'] != 'SELL':
                continue
            sells += 1
            if r['Dollar_PnL'].strip():
                audit_pnl += float(r['Dollar_PnL'])
            if (r.get('Commission') or '').strip():
                audit_fees += float(r['Commission'])
print(f"Booked SELL rows: {sells}")
print(f"Audit gross P&L:  ${audit_pnl:+.2f}")
print(f"Audit commissions:${audit_fees:.2f}")
print(f"Audit net:        ${audit_pnl - audit_fees:+.2f}")

print(f"\n=== RECONCILIATION ===")
print(f"IBKR net (realized): ${ibkr_realized - ibkr_commissions:+.2f}")
print(f"Audit net (booked):  ${audit_pnl - audit_fees:+.2f}")
print(f"Gap:                 ${(ibkr_realized - ibkr_commissions) - (audit_pnl - audit_fees):+.2f}")
print("A non-zero gap = trades the bot didn't book (orphans) or fill-price drift.")

ib.disconnect()
