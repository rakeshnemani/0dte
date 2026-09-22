"""Go-live status — mechanical GEX eval scoreboard.

Reads audit.csv, filters to the frozen-ruleset eval (mechanical `gex` trades from DAY0 forward — see
docs/GO_LIVE_MECH_GEX.md), and prints (1) a per-trade net-P&L ledger and (2) the live status of each
go-live gate (+$10k fee-adjusted / PF≥1.5 / 65% WR / sample / drawdown).

Pre-DAY0 trades are HISTORY and excluded (they ran under a different ruleset). Bump DAY0 whenever a
ruleset change resets the clock.

USAGE
    python scripts/go_live_status.py [YYYY-MM-DD]      # override DAY0 (default below)
"""
import csv, os, sys

DAY0 = sys.argv[1] if len(sys.argv) > 1 else "2026-09-13"   # eval clock start (ruleset frozen)
TARGET_PNL, TARGET_WR, TARGET_PF = 10000.0, 0.65, 1.5
TARGET_TRADES, TARGET_DAYS, MAX_DD = 40, 30, 2500.0

AUDIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audit.csv")


def _f(x):
    if x is None: return None
    s = str(x).replace("%", "").replace(",", "").strip()   # audit % cols carry a '%' suffix
    try: return float(s)
    except ValueError: return None


def main():
    rows = list(csv.DictReader(open(AUDIT)))
    # Eval trades = realized (has Dollar_PnL), mechanical gex, exit-date >= DAY0
    ev = [r for r in rows
          if r["Strategy"] == "gex" and _f(r.get("Dollar_PnL")) not in (None, 0.0)
          and r["Timestamp"][:10] >= DAY0]
    print(f"\n{'='*74}\n GO-LIVE STATUS — Mechanical GEX   (Day 0 = {DAY0}, frozen ruleset)\n{'='*74}")
    if not ev:
        print(f" No eval trades yet since {DAY0}. (Pre-Day-0 trades are history, excluded.)")
        return 0

    print(f" {'date':11}{'dir':>5}{'net$':>9}   exit")
    print(" " + "-"*54)
    cum = 0.0; curve = []; wins = []; losses = []; days = set()
    for r in ev:
        pnl = _f(r["Dollar_PnL"]); comm = _f(r.get("Commission")) or 0.0
        net = pnl - comm
        cum += net; curve.append(cum); days.add(r["Timestamp"][:10])
        (wins if net > 0 else losses).append(net)
        reason = (r.get("Reason") or "").split(":")[0][:28]
        print(f" {r['Timestamp'][:10]:11}{r['Direction']:>5}{net:>+9.0f}   {reason}")

    n = len(ev); nw = len(wins)
    wr = nw / n
    gp, gl = sum(wins), abs(sum(losses))
    pf = (gp / gl) if gl > 0 else None       # None = no losses yet → PF undefined (not a real pass)
    peak_eq = 0.0; dd = 0.0
    for c in curve:                       # max peak-to-trough drawdown of the equity curve
        peak_eq = max(peak_eq, c); dd = max(dd, peak_eq - c)
    expectancy = cum / n

    def bar(frac):
        f = max(0.0, min(1.0, frac)); return "█"*int(f*10) + "░"*(10-int(f*10))
    def mark(ok): return "✅" if ok else "  "

    pf_ok = pf is not None and pf >= TARGET_PF     # undefined (no losses yet) is NOT a pass
    pf_str = f"{pf:.2f}" if pf is not None else "n/a"
    print("\n GATE STATUS")
    print(f"  {mark(cum>=TARGET_PNL)} Cumulative net P&L : {cum:>+8.0f} / +{TARGET_PNL:,.0f}   {bar(cum/TARGET_PNL)}  ({cum/TARGET_PNL*100:.1f}%)")
    print(f"  {mark(pf_ok)} Profit factor      : {pf_str:>8} / ≥{TARGET_PF}" + ("   (no losses yet)" if pf is None else ""))
    print(f"  {mark(wr>=TARGET_WR)} Win rate           : {wr*100:>7.0f}% / ≥{TARGET_WR*100:.0f}%   ({nw}W/{n-nw}L)")
    print(f"  {mark(dd<=MAX_DD)} Max drawdown       : {dd:>8.0f} / ≤{MAX_DD:,.0f}")
    print(f"     Expectancy/trade   : {expectancy:>+8.0f}  (info — net P&L ÷ trades, not a separate gate)")
    print(f"  {mark(n>=TARGET_TRADES)} Trades             : {n:>8} / {TARGET_TRADES}   {bar(n/TARGET_TRADES)}")
    print(f"  {mark(len(days)>=TARGET_DAYS)} Trading days       : {len(days):>8} / {TARGET_DAYS}   {bar(len(days)/TARGET_DAYS)}")
    passed = sum([cum>=TARGET_PNL, pf_ok, wr>=TARGET_WR, dd<=MAX_DD, n>=TARGET_TRADES, len(days)>=TARGET_DAYS])
    print(f"\n  → {passed}/6 numeric gates passed. Full checklist (regime/reliability): docs/GO_LIVE_MECH_GEX.md")
    print(f"  → NOT live until every gate passes. This is the paper eval scoreboard.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
