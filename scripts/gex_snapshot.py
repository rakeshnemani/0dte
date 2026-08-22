"""One-shot GEX snapshot — the thesis-forming picture, on demand.

Connects to IBKR (its OWN clientId, so it runs alongside the bot), pulls the LIVE SPX option
chain, and prints the dealer-gamma picture we use to build a daily thesis: Gflip + regime,
net GEX, the top gamma-weighted support/resistance ladders, and the strike-by-strike shelf
around spot (so you can see where the shelf PEAKS and where it ENDS — the runway/buffer read).

Reuses the bot's exact chain fetch (broker.fetch_gex_chain) and math (src/gex.py), so the
numbers match what the bot computes live.

WHEN TO RUN IT
  - **Monday / any morning ~9:45–10:15 ET**, after the open. 0DTE OI starts near zero at 9:30
    and BUILDS through the morning — the walls/Gflip only firm up once OI has substance, and
    that's also when the 15-min opening range has formed. A pre-market or weekend run returns a
    stale/thin chain (Friday's OI carryover; the market is closed), so it is NOT useful for a
    fresh thesis. Running the *bot* over the weekend does nothing either — it just sleeps until
    the open and only collects GEX during market hours.

USAGE
    python scripts/gex_snapshot.py [SYMBOL]        # default SPX
"""
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import config
config.IBKR_CLIENT_ID = 13          # distinct from bot=1 / reconcile=9 / backfill=11

import csv                          # noqa: E402
import gex                          # noqa: E402
import market_time                  # noqa: E402
from broker import IBKRBroker       # noqa: E402

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "SPX"
GEX_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "gex")


def _save_chain(symbol, spot, chain):
    """Append this snapshot to data/gex/chain_<ET-date>.csv in the SAME 9-column format the bot
    writes (bot._save_gex_chain) — so gex_dashboard.py can render it even when the bot isn't
    running. Appends (matches the bot), so a bot-running day just gets one more snapshot row."""
    os.makedirs(GEX_DIR, exist_ok=True)
    path = os.path.join(GEX_DIR, f"chain_{market_time.now_et():%Y-%m-%d}.csv")
    new = not os.path.isfile(path)
    gf = gex.gamma_flip(chain, spot)
    ts = market_time.now_et().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "symbol", "spot", "gflip", "strike",
                        "oi_call", "oi_put", "iv", "T"])
        for c in chain:
            w.writerow([ts, symbol, round(spot, 2), round(gf, 2) if gf else "",
                        c["strike"], int(c["oi_call"]), int(c["oi_put"]),
                        round(c["iv"], 4), round(c["T"], 6)])
    return path


def main():
    b = IBKRBroker()
    try:
        b.connect()
    except Exception as e:
        print(f"❌ Could not connect to IBKR Gateway (port {config.IBKR_PORT}): {e}")
        print("   Is IB Gateway running? (the bot uses clientId 1; this script uses 13.)")
        return 1

    try:
        spot, chain = b.fetch_gex_chain(
            SYMBOL, config.GEX_CHAIN_STRIKE_PCT, config.GEX_CHAIN_EXPIRIES,
            config.GEX_CHAIN_MAX_STRIKES)
    finally:
        b.disconnect()

    if not chain or not spot:
        print(f"❌ Empty chain for {SYMBOL} — market likely closed, or OI not yet populated "
              f"(run after ~9:45 ET). Nothing to snapshot.")
        return 1

    saved = _save_chain(SYMBOL, spot, chain)

    gflip = gex.gamma_flip(chain, spot)
    regime = gex.gex_regime(spot, gflip)
    net_total = gex.net_gex(spot, chain) / 1e6
    net_0dte = gex.net_gex_0dte(spot, chain) / 1e6
    calls, puts = gex.gex_ladders(spot, chain, n=6)
    by_strike = gex.gex_by_strike(spot, chain)

    dist = (spot - gflip) / gflip * 100 if gflip else None
    print("\n" + "=" * 60)
    print(f" GEX SNAPSHOT — {SYMBOL}   (spot {spot:.2f})")
    print("=" * 60)
    print(f" Gflip        : {gflip:.2f}"
          + (f"   (spot {dist:+.2f}% vs flip → {regime.upper()} gamma)" if dist is not None else ""))
    print(f" Net GEX      : {net_total:,.0f}M total   |   {net_0dte:,.0f}M 0DTE")
    print()
    print(" RESISTANCE ladder (call side, most +GEX, heaviest first):")
    for k, v in calls:
        print(f"    {k:.0f}   {v/1e6:+9.1f}M")
    print(" SUPPORT ladder (put side, most -GEX, heaviest first):")
    for k, v in puts:
        print(f"    {k:.0f}   {v/1e6:+9.1f}M")

    # Strike-by-strike shelf around spot — where does it peak / thin out?
    lo, hi = spot - 45, spot + 45
    print(f"\n Shelf around spot ({lo:.0f}–{hi:.0f}) — gamma $ per strike:")
    band = sorted(k for k in by_strike if lo <= k <= hi)
    peak_k = max(band, key=lambda k: abs(by_strike[k])) if band else None
    for k in band:
        v = by_strike[k] / 1e6
        mark = "  <== spot" if abs(k - spot) <= 2.5 else ("  <== PEAK node" if k == peak_k else "")
        print(f"    {k:.0f}   {v:+9.1f}M{mark}")

    print("\n Read: the PEAK node is the heaviest wall; the shelf 'ends' where the $ drops off")
    print(" sharply above/below it. Runway = enter BEYOND the shelf (PUT below support / CALL")
    print(" above resistance); IntoWall = enter into it. Add a small buffer past the shelf edge.")
    print("=" * 60)
    print(f" saved chain snapshot → {os.path.relpath(saved)}")
    print(" render the visual:     python scripts/gex_dashboard.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
