"""Read-only Gateway validation for a cash-settled index-option migration. NO orders.

Generalised: pass the symbol as an arg (default XSP).
    python scripts/validate_xsp.py SPX      # validate the SPX switch
    python scripts/validate_xsp.py XSP       # (default) re-check XSP

Exercises the real IBKRBroker code path the bot uses:
  1. Qualify the underlying Index + fetch its level.
  2. Pull the option-chain definition (reqSecDefOptParams) — proves the chain exists
     and reports exchange / tradingClass / multiplier / expirations / strikes.
  3. Qualify a concrete ATM 0DTE option via broker.get_option_contract, trying the
     configured option_exchange first, then the other of {CBOE, SMART}.
  4. Price an ATM vertical via broker.get_spread_value + per-leg bid/ask (fill
     quality / fees proxy). Live quotes need market hours.
  5. Confirm the signal source bars (SIGNAL_SOURCE → SPY).

clientId=13 (safe alongside the running bot on clientId=1).
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import config
config.IBKR_CLIENT_ID = 13

import market_time
from ib_insync import Option, LimitOrder
from broker import IBKRBroker, INDEX_SPECS

SYM = (sys.argv[1] if len(sys.argv) > 1 else 'XSP').upper()
if SYM not in INDEX_SPECS:
    raise SystemExit(f"{SYM} is not a known cash-settled index (INDEX_SPECS: {list(INDEX_SPECS)})")

b = IBKRBroker()
try:
    b.connect()
except Exception as e:
    raise SystemExit(f"Could not connect to IBKR: {e}")

print("\n" + "=" * 68)
print(f"{SYM} MIGRATION — Gateway validation (read-only, no orders)")
print("=" * 68)


def hdr(n, t):
    print(f"\n[{n}] {t}")


# ── 1. underlying ────────────────────────────────────────────────────────────
hdr(1, f"{SYM} underlying (Index)")
u = b.underlying_contract(SYM)
q = b.ib.qualifyContracts(u)
if not q:
    print(f"  ✗ FAILED to qualify {SYM} index — stop here, chain won't work.")
    b.disconnect(); sys.exit(1)
u = q[0]
print(f"  ✓ qualified: conId={u.conId} secType={u.secType} exchange={u.exchange} "
      f"primaryExchange={getattr(u,'primaryExchange','')}")
px = b.get_current_price(SYM)
print(f"  {SYM} level (mid): {px}  {'(after-hours — may be 0/stale)' if px <= 0 else ''}")

# ── 2. option-chain definition ───────────────────────────────────────────────
hdr(2, f"{SYM} option chain (reqSecDefOptParams)")
params = b.ib.reqSecDefOptParams(SYM, '', 'IND', u.conId)
root = b.option_symbol(SYM)
want_exch = b.option_exchange(SYM)
if not params:
    print(f"  ✗ No option parameters returned for {SYM}.")
else:
    for p in params:
        exps = sorted(p.expirations)
        strikes = sorted(p.strikes)
        print(f"  exchange={p.exchange} tradingClass={p.tradingClass} multiplier={p.multiplier}")
        print(f"    expirations: {len(exps)} (first few: {exps[:5]})")
        print(f"    strikes: {len(strikes)} (range {strikes[0] if strikes else '-'}"
              f"..{strikes[-1] if strikes else '-'}"
              + (f", ATM step ~{strikes[1]-strikes[0]}" if len(strikes) > 1 else "") + ")")
    print(f"  → configured option_exchange '{want_exch}' present: "
          f"{'✓' if any(p.exchange == want_exch for p in params) else '✗ (try the other of CBOE/SMART)'}")
    print(f"  → tradingClass '{root}' present: "
          f"{'✓' if any(p.tradingClass == root for p in params) else '✗'}")

# ── 3. qualify a concrete ATM 0DTE option (real broker path) ─────────────────
hdr(3, "Qualify an ATM option via broker.get_option_contract")
all_exps = sorted({e for p in params for e in p.expirations}) if params else []
today = market_time.now_et().strftime('%Y%m%d')
future = [e for e in all_exps if e >= today]
target_exp = today if today in all_exps else (future[0] if future else (all_exps[-1] if all_exps else today))
all_strikes = sorted({s for p in params for s in p.strikes}) if params else []
ref = px if px > 0 else (all_strikes[len(all_strikes)//2] if all_strikes else 0)
atm = min(all_strikes, key=lambda s: abs(s - ref)) if all_strikes else round(ref)
print(f"  today={today} target_exp={target_exp} ref_level={ref} ATM_strike={atm} "
      f"(config STRIKE_STEP={config.STRIKE_STEP.get(SYM)}, SPREAD_WIDTH={config.SPREAD_WIDTH.get(SYM)})")

def try_qualify(exch):
    c = Option(root, target_exp, atm, 'P', exch, tradingClass=root, multiplier='100', currency='USD')
    return b.ib.qualifyContracts(c)

# try the configured exchange first, then the other
order = [want_exch] + [e for e in ('CBOE', 'SMART') if e != want_exch]
for exch in order:
    try:
        r = try_qualify(exch)
        if r:
            print(f"  ✓ qualified ATM PUT on {exch}: conId={r[0].conId} "
                  f"{r[0].localSymbol} lastTradeDate={r[0].lastTradeDateOrContractMonth}")
            if exch != want_exch:
                print(f"  ⚠ configured option_exchange is '{want_exch}' but it qualified on "
                      f"'{exch}' — update INDEX_SPECS['{SYM}']['option_exchange'] to '{exch}'.")
            break
        print(f"  ✗ {exch}: no qualifying contract")
    except Exception as e:
        print(f"  ✗ {exch}: {e}")

# ── 4. price an ATM vertical + per-leg bid/ask (fill quality) ────────────────
hdr(4, "Price an ATM vertical (broker.get_spread_value) + per-leg quotes")
sv = None   # spread mid-cost; stays None after-hours (used by 4b's whatIf limit)
if target_exp == today and px > 0:
    try:
        long_k, short_k = atm, atm - config.SPREAD_WIDTH.get(SYM, 1)
        sv = b.get_spread_value(SYM, 'PUT', long_k, short_k)
        print(f"  vertical {long_k}/{short_k} PUT mid-cost: {sv} "
              f"(~${sv*100:.0f} debit / spread; max loss = debit)")
        for k in (long_k, short_k):
            oc = b.get_option_contract(SYM, 'PUT', k)
            tk = b.ib.reqMktData(oc, '', snapshot=False, regulatorySnapshot=False)
            b.ib.sleep(2.5)
            spread = (tk.ask - tk.bid) if (tk.bid == tk.bid and tk.ask == tk.ask
                                           and tk.bid not in (-1, None)) else None
            print(f"    {k}P  bid={tk.bid} ask={tk.ask} last={tk.last}"
                  + (f"  (bid/ask spread ${spread:.2f})" if spread is not None
                     else "  (no live data — after hours?)"))
            b.ib.cancelMktData(oc)
    except Exception as e:
        print(f"  (pricing skipped: {e})")
else:
    print(f"  target expiry {target_exp} vs today {today}, level {px} — live 0DTE pricing "
          f"needs market hours + a same-day expiry. Re-run intraday for fill-quality numbers.")

# ── 4b. BAG combo order validation (#41 — the real order path; NO order placed) ─
hdr("4b", "Combo (BAG) order check via whatIfOrder — validates + prints est. commission")
try:
    long_k, short_k = atm, atm - config.SPREAD_WIDTH.get(SYM, 1)
    long_c = b.get_option_contract(SYM, 'PUT', long_k)
    short_c = b.get_option_contract(SYM, 'PUT', short_k)
    bag = b.make_bag(SYM, long_c.conId, short_c.conId)
    leg_syms = {cd.symbol for cd in (long_c, short_c)}
    sym_ok = {bag.symbol} == leg_syms
    # Deterministic #41 check (works any time): the BAG's symbol must equal the legs'
    # underlying — the SPXW-vs-SPX mismatch is exactly what error 478 rejected.
    print(f"  bag.symbol={bag.symbol!r} vs leg underlyings={leg_syms} "
          f"exchange={bag.exchange}  {'✓ match (#41 fixed)' if sym_ok else '✗ MISMATCH → 478'}")
    # whatIf runs IBKR's real order validation (the path that threw 478) without
    # placing it; the OrderState carries the est. commission — but it needs market
    # hours (live pricing) to return a populated state.
    st = b.ib.whatIfOrder(bag, LimitOrder('BUY', 1, round(max(sv or 1.0, 0.05), 2)))
    comm = getattr(st, 'minCommission', None)
    if comm not in (None, '', float('inf')):
        print(f"  ✓ combo ACCEPTED (whatIf). Est. commission ≈ "
              f"${st.minCommission}–${getattr(st,'maxCommission','?')} "
              f"{getattr(st,'commissionCurrency','')} · initMargin Δ {getattr(st,'initMarginChange','?')}")
        print(f"    → round-trip (open+close) ≈ 2× that. This is the #42 fee number.")
    elif sym_ok:
        print(f"  … whatIf returned an empty state (no est. commission) — expected "
              f"AFTER-HOURS/no live pricing. Symbol check passed, so #41 is fixed; "
              f"re-run intraday for combo acceptance + the commission number.")
    else:
        print(f"  ✗ combo REJECTED and symbols MISMATCH — the #41 bug is present.")
except Exception as e:
    print(f"  ✗ combo whatIf failed: {e}")

# ── 5. signal source ─────────────────────────────────────────────────────────
src = config.SIGNAL_SOURCE.get(SYM, SYM)
hdr(5, f"Signal source — {src} 1-min bars (config.SIGNAL_SOURCE['{SYM}'])")
df = b.fetch_intraday_data(src)
print(f"  source={src}  bars={len(df)}  "
      f"{'✓ VWAP source OK' if not df.empty else '✗ no bars (after hours / no data)'}")

print("\n" + "=" * 68)
print("Done. Contract/chain checks are valid any time; live bid/ask needs market hours.")
print("=" * 68)
b.disconnect()
