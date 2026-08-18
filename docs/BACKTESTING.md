# Backtesting & Analysis Tooling

How we test hypotheses against history, what data feeds it, and the caveats every result
must respect. **Read this before quoting or extending any backtest.**

> **⚠️ 2026-08-17:** the breakout-era analysis scripts described below — `backtest_39.py`,
> `flip_analysis.py`, `replay_invalidation.py`, `validate_xsp.py` — were **DELETED** with the
> breakout strategy. The live system is single-leg **trend + gex**. The trend backtest is
> `scripts/backtest_spread_dollars.py` (real Black-Scholes legs, per-quarter + per-trade CSV;
> `pull_spx_2y.py` builds the 3-yr SPX 1-min cache). **GEX has no backtest** — no free historical
> GEX data exists, so it is forward-test-only. The sections below are kept as history of the
> breakout investigation.

## The scripts

All live in `scripts/`, all read-only against IBKR (historical bars only), each on its own
`clientId` so they run alongside the bot (bot=1). Gateway must be up on port 4002.

| script | what it answers | clientId |
|---|---|---|
| `backtest_39.py` | **The main engine.** Re-derives entry signals minute-by-minute from bars (no look-ahead), models the bot's gates (one-position-per-symbol, 30-min cooldown, min-conviction), simulates outcomes. Compares config scenarios (baseline / initial-only / flipped) **and** the regime split (chop vs trend). | 15 |
| `flip_analysis.py [YYYY-MM-DD]` | Replays the **actual audit entries** as-taken vs direction-flipped, over a date window. Quick "are we on the wrong side?" check. | 14 |
| `replay_invalidation.py` | Replays actual audit entries under different **exit** configs (`VWAP_INVALIDATION_*`), using the REAL `strategy.thesis_invalidated`. | 12 |
| `validate_xsp.py SYM` | Gateway validation of a cash-settled index product (SPX/XSP): qualifies the index + 0DTE chain, prices an ATM vertical, `[4b]` whatIf-validates the **BAG combo** and prints **estimated commission**. NO orders placed. | 13 |
| `counterfactual.py SYMBOL HH:MM` | "What did SYMBOL do after this ET time today?" — for retros. | 99 |

Unit tests (no Gateway): `test_xsp_and_regime.py` (contract mapping, #32 exit, #41 bag, flip),
`test_entry_timeout.py` (#34), `test_close_integrity.py` (#30).

## Data inputs

1. **`audit.csv`** (committed — the durable ledger). One row per fill. Columns: `Timestamp,
   Action, Symbol, Direction, Price, Underlying_Price, ADX, VWAP, ORB_High, ORB_Low, Breadth,
   Reason, Profit_Pct, Dollar_PnL, ADX_Slope, Peak_Pct, Conviction, Commission, PermId`.
   - **Direction is the EXECUTED side.** When the flip is on, the `Reason` carries
     `FLIP #42: signal CALL → exec PUT` so both the original signal and the executed side are
     recoverable. Conviction/ADX/VWAP/ORB are the entry-signal values.
   - `RECONCILE` rows are annotations, not trades — skip them (`Action` filter).
2. **IBKR 1-min historical bars** — refetched live per run. **SPY is the signal source** for
   both XSP and SPX (index bars are volumeless → bad VWAP; `config.SIGNAL_SOURCE` maps them to
   SPY). IBKR retains 1-min history for months, so re-fetch is fine.
3. **Config history** — ⚠️ `.env` is git-ignored, so the *live* config per day is NOT in git.
   It's narrated in `docs/RETROSPECTIVE.md` (dated) and in the `.env`/`config.py` comments.
   When reconstructing "what was live on day X," check the retro for that date. Backtest
   scripts set config explicitly, so they don't depend on this — but retros do.

## Caveats — every backtest result is subject to these

1. **Proxy P&L, not real option economics.** Outcome = direction-adjusted **underlying** move
   in basis points, capped at TP `+40 bp` (≈ +60% on an ATM spread) and STOP `−55 bp` (≈ −70%).
   It approximates gross spread P&L; it is not exact.
2. **Fees EXCLUDED.** Every headline bp is *gross*. Overlay fees separately (SPX ≈ **2 bp/trade**:
   ~$2.15 debit, ~$0.10/leg fills, so ~$6 round-trip ≈ 2 bp on a 1-contract trade). This is
   the make-or-break for thin edges like the flip (~+2 bp/trade gross).
3. **Single-symbol SPY reconstruction.** `backtest_39.py` re-derives signals from SPY bars only
   (the signal source), not the actual multi-symbol (SPY/QQQ/IWM/XSP) book — so its trade count
   differs from the real ledger. Fine for *relative* comparisons; not a replica of history.
4. **Two different VWAPs — do not mix them.** The bot's `thesis_invalidated`/entry use `ta`
   **rolling-14** VWAP (hugs price). The regime classifier uses **session-cumulative** VWAP
   (stable line, for counting crosses). The original `replay_invalidation.py` used cumulative
   VWAP by mistake and produced a wrong "N=6 optimal" number — now fixed to call the real rule.
5. **No look-ahead.** Signal-replay only ever passes bars up to the current entry bar.
6. **Small sample.** ~20 trading days, ~23–52 trades per scenario; sub-splits (e.g. chop vs
   trend, ~10–13 each) are **noisy** — treat as directional, not decisive.
7. **Not modeled:** the invalidation-throttle, the daily-trade cap, and the trailing stop
   (winners are capped at the +40 bp TP proxy). Cooldown and one-position-per-symbol *are*.

## Key findings on the books (so they aren't re-derived wrong)

- **Direction is systematically wrong.** As-taken 39% win / −9 bp; **flipped 61% / +50 bp**
  (BASELINE, `backtest_39.py`). The flip (#42) is the first positive-*gross* signal.
- **The entry filters are NOT the bottleneck (#39).** Un-blocking them makes it worse
  (39%→35%, −9→−134 bp); the guards are net-helpful and the invalidation exit is net-protective
  (−9 with it vs −63 without). Don't loosen them.
- **Regime-router not justified.** Flip wins in chop (60% vs 20% follow) but is a *wash* in
  trend (62% vs 54% follow, +33 bp both) — no regime where following wins, so routing buys
  nothing. Keep plain BASELINE-flipped.
- **Fees are the wall.** Even a +17% winner netted $0.78/lot on XSP (fees ate 87%); SPX cuts
  fees ~4×, which is why the thin flip edge is only *plausible* on SPX.

## Reproduce
```bash
python scripts/backtest_39.py            # main: scenarios + regime split
python scripts/flip_analysis.py 2026-07-13   # flip, windowed
python scripts/validate_xsp.py SPX       # intraday → real commission (the #42 gate)
```
