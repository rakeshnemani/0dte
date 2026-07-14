"""Read-only Gateway validation for the XSP migration (#3). NO orders placed.

Exercises the real IBKRBroker code path the bot uses:
  1. Qualify the XSP underlying (Index) + fetch its level.
  2. Pull the XSP option-chain definition (reqSecDefOptParams) — proves the chain
     exists and reports exchange / tradingClass / multiplier / expirations / strikes.
  3. Qualify a specific ATM 0DTE option via broker.get_option_contract (CBOE),
     with a SMART fallback if CBOE fails.
  4. Price an ATM vertical via broker.get_spread_value + per-leg bid/ask (fill
     quality). Live quotes need market hours; after-hours this may be empty.
  5. Confirm the signal source: fetch SPY 1-min bars (existing path).

Run:  python scripts/validate_xsp.py       clientId=13 (safe alongside a stopped bot)
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import os
import sys
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import config
config.IBKR_CLIENT_ID = 13   # distinct socket; bot is stopped but be safe

import market_time
from ib_insync import Option
from broker import IBKRBroker, INDEX_SPECS

b = IBKRBroker()
try:
    b.connect()
except Exception as e:
    raise SystemExit(f"Could not connect to IBKR: {e}")

print("\n" + "=" * 68)
print("XSP MIGRATION — Gateway validation (read-only, no orders)")
print("=" * 68)


def hdr(n, t):
    print(f"\n[{n}] {t}")


# ── 1. underlying ────────────────────────────────────────────────────────────
hdr(1, "XSP underlying (Index)")
u = b.underlying_contract('XSP')
q = b.ib.qualifyContracts(u)
if not q:
    print("  ✗ FAILED to qualify XSP index — stop here, chain won't work.")
    b.disconnect(); sys.exit(1)
u = q[0]
print(f"  ✓ qualified: conId={u.conId} secType={u.secType} exchange={u.exchange} "
      f"primaryExchange={getattr(u,'primaryExchange','')}")
xsp_px = b.get_current_price('XSP')
print(f"  XSP level (mid): {xsp_px}  {'(after-hours — may be 0/stale)' if xsp_px <= 0 else ''}")

# ── 2. option-chain definition ───────────────────────────────────────────────
hdr(2, "XSP option chain (reqSecDefOptParams)")
params = b.ib.reqSecDefOptParams('XSP', '', 'IND', u.conId)
if not params:
    print("  ✗ No option parameters returned for XSP.")
else:
    for p in params:
        exps = sorted(p.expirations)
        strikes = sorted(p.strikes)
        print(f"  exchange={p.exchange} tradingClass={p.tradingClass} multiplier={p.multiplier}")
        print(f"    expirations: {len(exps)} (first few: {exps[:5]})")
        print(f"    strikes: {len(strikes)} (range {strikes[0] if strikes else '-'}"
              f"..{strikes[-1] if strikes else '-'})")
    cboe = [p for p in params if p.exchange == 'CBOE']
    print(f"  → CBOE present: {'✓ yes' if cboe else '✗ NO (flip option_exchange to SMART)'}")
    print(f"  → tradingClass 'XSP' present: "
          f"{'✓' if any(p.tradingClass == 'XSP' for p in params) else '✗'}")

# ── 3. qualify a concrete ATM 0DTE option (real broker path) ─────────────────
hdr(3, "Qualify an ATM option via broker.get_option_contract")
# choose expiry: today if listed, else the nearest listed >= today
all_exps = sorted({e for p in params for e in p.expirations}) if params else []
today = market_time.now_et().strftime('%Y%m%d')
future = [e for e in all_exps if e >= today]
target_exp = today if today in all_exps else (future[0] if future else (all_exps[-1] if all_exps else today))
all_strikes = sorted({s for p in params for s in p.strikes}) if params else []
ref = xsp_px if xsp_px > 0 else (all_strikes[len(all_strikes)//2] if all_strikes else 0)
atm = min(all_strikes, key=lambda s: abs(s - ref)) if all_strikes else round(ref)
print(f"  today={today} target_exp={target_exp} ref_level={ref} ATM_strike={atm}")

def try_qualify(exch):
    root = b.option_symbol('XSP')
    c = Option(root, target_exp, atm, 'P', exch, tradingClass=root, multiplier='100', currency='USD')
    return b.ib.qualifyContracts(c)

opt = None
for exch in ('CBOE', 'SMART'):
    try:
        r = try_qualify(exch)
        if r:
            opt = r[0]
            print(f"  ✓ qualified ATM PUT on {exch}: conId={opt.conId} "
                  f"{opt.localSymbol} lastTradeDate={opt.lastTradeDateOrContractMonth}")
            break
        print(f"  ✗ {exch}: no qualifying contract")
    except Exception as e:
        print(f"  ✗ {exch}: {e}")

# ── 4. price an ATM vertical + per-leg bid/ask (fill quality) ────────────────
hdr(4, "Price an ATM vertical (broker.get_spread_value) + per-leg quotes")
if target_exp == today:
    try:
        step = config.STRIKE_STEP.get('XSP', 1)
        long_k, short_k = atm, atm - config.SPREAD_WIDTH.get('XSP', 1)
        sv = b.get_spread_value('XSP', 'PUT', long_k, short_k)
        print(f"  vertical {long_k}/{short_k} PUT mid-cost: {sv}")
        for k in (long_k, short_k):
            oc = b.get_option_contract('XSP', 'PUT', k)
            tk = b.ib.reqMktData(oc, '', snapshot=False, regulatorySnapshot=False)
            b.ib.sleep(2.5)
            print(f"    {k}P  bid={tk.bid} ask={tk.ask} last={tk.last} "
                  f"{'(no live data — after hours?)' if (tk.bid != tk.bid or tk.bid in (-1, None)) else ''}")
            b.ib.cancelMktData(oc)
    except Exception as e:
        print(f"  (pricing skipped: {e})")
else:
    print(f"  target expiry {target_exp} != today {today} — live 0DTE pricing needs "
          f"market hours + a same-day expiry. Re-run intraday for fill-quality numbers.")

# ── 5. signal source ─────────────────────────────────────────────────────────
hdr(5, "Signal source — SPY 1-min bars (config.SIGNAL_SOURCE['XSP'])")
src = config.SIGNAL_SOURCE.get('XSP', 'XSP')
df = b.fetch_intraday_data(src)
print(f"  source={src}  bars={len(df)}  "
      f"{'✓ VWAP source OK' if not df.empty else '✗ no bars (after hours / no data)'}")

print("\n" + "=" * 68)
print("Done. Contract/chain checks are valid any time; live bid/ask needs market hours.")
print("=" * 68)
b.disconnect()
