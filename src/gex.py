"""GEX — dealer Gamma Exposure calculator (pure functions, no I/O).

Turns an options chain (open interest by strike + IV) into the two things the GEX
strategy needs: the **gamma-flip level** (Gflip) and the **concentration zones**
(OI walls). Everything here is math in → numbers out, so it's unit-testable without
IBKR; the live chain is fetched in broker.py and fed in as `chain`.

Convention (the common "dealer" sign): dealers are assumed **long call gamma, short
put gamma**, so net dealer gamma at spot S is
    Σ_strikes ( OI_call − OI_put ) · Γ(S, K, T, IV) · S² · 0.01 · 100
- **Below Gflip → net negative gamma**: dealers hedge WITH the move (amplify) → momentum-
  friendly (breakouts follow through). This is the regime the strategy wants to trade.
- **Above Gflip → net positive gamma**: dealers fade the move (dampen) → chop/mean-revert.

Honest caveats: (1) the dealer sign is an assumption, not observed positioning;
(2) open interest is a once-a-day settled number (prior session) — no intraday OI;
(3) Γ per strike uses whatever IV we pass (IBKR's per-strike IV live, else an estimate).
"""
from math import exp, log, pi, sqrt
from typing import List, Optional


def bs_gamma(S: float, K: float, T: float, sigma: float) -> float:
    """Black-Scholes gamma (same for a call and a put at the same strike/T/IV). r=0."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1 = (log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt(T))
    return exp(-0.5 * d1 * d1) / (S * sigma * sqrt(2.0 * pi * T))


def net_gex(spot: float, chain: List[dict], mult: int = 100) -> float:
    """Net dealer GEX at `spot`, in $ per 1% move. `chain` = list of per-strike dicts
    with keys: strike, oi_call, oi_put, iv, T (years to expiry). Sign: calls +, puts −."""
    total = 0.0
    for c in chain:
        g = bs_gamma(spot, c['strike'], c['T'], c['iv'])
        total += (c['oi_call'] - c['oi_put']) * g
    return total * spot * spot * 0.01 * mult


def gamma_flip(chain: List[dict], spot: float,
               lo: float = 0.92, hi: float = 1.08, steps: int = 320) -> Optional[float]:
    """Scan spot·lo … spot·hi for zero-crossings of net_gex and return the flip price
    CLOSEST to the current spot (the relevant regime boundary). None if no crossing."""
    if not chain or spot <= 0:
        return None
    xs = [spot * lo + (spot * hi - spot * lo) * i / steps for i in range(steps + 1)]
    prev_x = xs[0]
    prev_g = net_gex(prev_x, chain)
    crossings = []
    for x in xs[1:]:
        g = net_gex(x, chain)
        if (prev_g < 0 <= g) or (prev_g > 0 >= g):
            if g != prev_g:                                   # linear-interpolate the crossing
                crossings.append(prev_x + (x - prev_x) * (0 - prev_g) / (g - prev_g))
            else:
                crossings.append((prev_x + x) / 2)
        prev_x, prev_g = x, g
    if not crossings:
        return None
    return min(crossings, key=lambda f: abs(f - spot))


def gex_regime(spot: float, gflip: Optional[float]) -> str:
    """'negative' (spot < Gflip → dealers amplify → momentum) / 'positive' (dampen) /
    'unknown' (no flip found)."""
    if gflip is None:
        return 'unknown'
    return 'negative' if spot < gflip else 'positive'


def concentration_zones(chain: List[dict], n: int = 3) -> dict:
    """The heaviest open-interest strikes — the 'walls' price tends to chop around. OI is
    summed by strike ACROSS expiries first (the chain may hold multiple expiries per strike).
    Returns {'call_walls': [(strike, oi), ...], 'put_walls': [...]} sorted by OI desc."""
    by_strike = {}
    for c in chain:
        s = by_strike.setdefault(c['strike'], {'oi_call': 0.0, 'oi_put': 0.0})
        s['oi_call'] += c['oi_call']; s['oi_put'] += c['oi_put']
    calls = sorted(by_strike.items(), key=lambda kv: kv[1]['oi_call'], reverse=True)[:n]
    puts = sorted(by_strike.items(), key=lambda kv: kv[1]['oi_put'], reverse=True)[:n]
    return {
        'call_walls': [(k, v['oi_call']) for k, v in calls],
        'put_walls': [(k, v['oi_put']) for k, v in puts],
    }


def net_gex_0dte(spot: float, chain: List[dict], mult: int = 100) -> float:
    """Net dealer GEX from ONLY the nearest-expiry (0DTE) strikes in the chain — the
    same units as net_gex, but restricted to today's expiry (T at its minimum)."""
    if not chain:
        return 0.0
    min_T = min(c['T'] for c in chain)
    return net_gex(spot, [c for c in chain if c['T'] <= min_T + 1e-9], mult)


def gex_by_strike(spot: float, chain: List[dict], mult: int = 100) -> dict:
    """Per-strike net dealer GEX ($, our convention), summed across expiries. Positive =
    dealers dampen there (resistance / call wall); negative = amplify (support / put wall)."""
    agg = {}
    for c in chain:
        g = bs_gamma(spot, c['strike'], c['T'], c['iv'])
        agg[c['strike']] = agg.get(c['strike'], 0.0) + (c['oi_call'] - c['oi_put']) * g
    return {k: v * spot * spot * 0.01 * mult for k, v in agg.items()}


def gex_walls(spot: float, chain: List[dict], mult: int = 100):
    """(call_wall_strike, call_wall_$, put_wall_strike, put_wall_$) — the strikes with the
    largest POSITIVE (dealer resistance) and most NEGATIVE (dealer support) net GEX. This is
    the 'GEX wall' in the dealer-gamma sense (matches external GEX providers), NOT raw-OI
    concentration (that's concentration_zones, which the wall-breakout entry uses)."""
    agg = gex_by_strike(spot, chain, mult)
    if not agg:
        return (None, 0.0, None, 0.0)
    cw = max(agg.items(), key=lambda kv: kv[1])   # most positive
    pw = min(agg.items(), key=lambda kv: kv[1])   # most negative
    return (cw[0], cw[1], pw[0], pw[1])


def near_a_wall(price: float, zones: dict, tol: float) -> bool:
    """True if `price` is within `tol` (price units) of any concentration wall — the
    choppy area the strategy wants to be BREAKING OUT of, not sitting inside."""
    walls = [s for s, _ in zones.get('call_walls', [])] + [s for s, _ in zones.get('put_walls', [])]
    return any(abs(price - w) <= tol for w in walls)
