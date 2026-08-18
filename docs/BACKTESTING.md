# Backtesting & Analysis Tooling

How the trend strategy was validated against history, what data feeds it, and the caveats every
result must respect. **Read this before quoting or extending any backtest.**

## The trend backtest — `scripts/backtest_dollars.py`

Re-derives the Supertrend/PSAR/Kaufman signal minute-by-minute (no look-ahead) over a 3-year SPX
1-min cache and prices each trade with real Black-Scholes legs. Outputs per-quarter and per-trade
CSVs. `scripts/pull_spx_2y.py` builds the cache (`.spx_1min_2y_cache.pkl`, 2023-07 → present) from
IBKR historical bars.

Headline (in-sample): single-leg net **+$12,985 / 3yr (t≈1.3)**, vs the old two-leg structure's +$1,328 — the
fee-halving + uncapped convex tail is where the edge is. Read every number through the caveats below.

## GEX has no backtest

No free historical GEX (dealer-gamma) data exists, so GEX is **forward-test only**. The bot saves the
live OI/IV chain to `data/gex/chain_YYYY-MM-DD.csv` on every refresh to accumulate our own dataset for
a future backtest.

## Data inputs

1. **3-year SPX 1-min cache** (`.spx_1min_2y_cache.pkl`) — built by `pull_spx_2y.py` from IBKR.
2. **`audit.csv`** — the durable live ledger (one row per fill; permId joins each row to IBKR).
3. **`.env` is git-ignored**, so the *live* config per day is narrated in `docs/RETROSPECTIVE.md`.
   Backtest scripts set their config explicitly, so they don't depend on this.

## Caveats — every result is subject to these

1. **In-sample.** The +$12,985 is in-sample, ~80% from 2025, and still can't clear 2024. **t≈1.3 is
   not statistically significant** — a promising-but-unproven candidate, not a found edge.
2. **Fees.** Every headline is *gross*; overlay fees separately (~$1.63/side on SPX single-leg —
   single-leg halves the round-trip vs a two-leg structure).
3. **Black-Scholes approximation.** Legs are priced with a BS model + an IV assumption, not exact fills.
4. **No look-ahead.** Signal-replay only ever passes bars up to the current entry bar.
5. **Small, regime-dependent sample.** Per-quarter counts are noisy — treat splits as directional.

## Reproduce

```bash
python scripts/pull_spx_2y.py        # build/refresh the 3-yr SPX 1-min cache (once)
python scripts/backtest_dollars.py   # per-quarter + per-trade trend backtest
```

> The breakout-era analysis scripts (`backtest_39`, `flip_analysis`, `replay_invalidation`,
> `validate_xsp`) were **deleted** with the breakout strategy on 2026-08-17.
