"""Unit tests for src/gex.py — the dealer-GEX calculator (Gflip, regime, walls).

Pure math, no IBKR. Builds synthetic option chains with known gamma structure and
checks the flip level, regime classification, and concentration zones.

Run:  python scripts/test_gex.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import gex

_fails = []


def check(name, cond):
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}")
    if not cond:
        _fails.append(name)


T = 0.5 / 252          # ~half a trading day to expiry (0DTE-ish)
IV = 0.15


def chain(oi_call_fn, oi_put_fn, center=6000, span=300, step=25):
    return [{'strike': k, 'oi_call': oi_call_fn(k), 'oi_put': oi_put_fn(k), 'iv': IV, 'T': T}
            for k in range(center - span, center + span + 1, step)]


def test_bs_gamma():
    print("\nbs_gamma — ATM peak, positive, expiry-safe")
    atm = gex.bs_gamma(6000, 6000, T, IV)
    otm = gex.bs_gamma(6000, 6100, T, IV)
    check("gamma > 0 at ATM", atm > 0)
    check("ATM gamma > OTM gamma", atm > otm)
    check("T<=0 → gamma 0 (no crash)", gex.bs_gamma(6000, 6000, 0, IV) == 0.0)
    check("sigma<=0 → gamma 0", gex.bs_gamma(6000, 6000, T, 0) == 0.0)


def test_flip_and_regime():
    print("\ngamma_flip / gex_regime — put-heavy below, call-heavy above → flip ~center")
    # oi_call rises with strike, oi_put falls — symmetric around 6000
    c = chain(lambda k: (k - 5700) / 25.0, lambda k: (6300 - k) / 25.0)
    flip = gex.gamma_flip(c, spot=6000)
    check(f"flip found near center (got {flip})", flip is not None and 5960 <= flip <= 6040)
    check("spot below flip → negative-gamma (momentum) regime",
          gex.gex_regime(flip - 50, flip) == 'negative')
    check("spot above flip → positive-gamma (chop) regime",
          gex.gex_regime(flip + 50, flip) == 'positive')
    check("no flip → 'unknown'", gex.gex_regime(6000, None) == 'unknown')


def test_net_gex_sign():
    print("\nnet_gex — sign follows call/put OI imbalance")
    puts = chain(lambda k: 0.0, lambda k: 100.0)      # puts only → dealers short gamma
    calls = chain(lambda k: 100.0, lambda k: 0.0)     # calls only → dealers long gamma
    check("put-only chain → net GEX < 0", gex.net_gex(6000, puts) < 0)
    check("call-only chain → net GEX > 0", gex.net_gex(6000, calls) > 0)
    check("put-only chain has NO positive flip (all negative)",
          gex.gamma_flip(puts, 6000) is None)


def test_concentration_zones():
    print("\nconcentration_zones / near_a_wall — heaviest OI strikes")
    def oc(k): return 5000 if k == 6100 else 50
    def op(k): return 8000 if k == 5900 else 50
    c = chain(oc, op)
    z = gex.concentration_zones(c, n=2)
    check("top call wall = 6100", z['call_walls'][0][0] == 6100)
    check("top put wall = 5900", z['put_walls'][0][0] == 5900)
    check("price at a wall detected", gex.near_a_wall(6098, z, tol=5) is True)
    check("price away from walls not flagged", gex.near_a_wall(6000, z, tol=5) is False)


if __name__ == '__main__':
    test_bs_gamma()
    test_flip_and_regime()
    test_net_gex_sign()
    test_concentration_zones()
    print()
    if _fails:
        print(f"❌ {len(_fails)} FAILED: {_fails}")
        sys.exit(1)
    print("✅ all GEX calculator tests passed")
