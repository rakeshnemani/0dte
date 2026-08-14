# CLAUDE.md — 0DTE Trading Bot

Orientation for working on this repo. Read the linked docs for depth; this file is the map.

## What this is

A Python **0DTE options-spread trading bot** on **Interactive Brokers paper trading**
(via `ib_insync`). Trades **SPY, QQQ, IWM**. Two regime-matched structures:
- **Directional debit spreads** (CALL/PUT verticals) on trend days.
- **Iron condors** (sell premium) on range/chop days.

**Goal:** reach consistent, *fee-adjusted* profitability on paper, then go live.

**Status (2026-08-10): running BOTH `trend` + `gex` at once (`STRATEGY=trend,gex`), SPX single-leg,
9:30–3:55.** Dual-strategy engine: trades keyed `strategy:symbol` (e.g. `trend:SPX`, `gex:SPX`) so each
strategy holds its own position; both entry scans run every loop; exits route per `trade['strategy']`;
orphan/anti-cascade guards gather conIds across BOTH (a trade's own conIds distinguish it); per-strategy
cooldowns; shared account guards; `audit.csv` has a `Strategy` column tagging every row. GEX is
forward-test-only (no historical GEX data) — Gflip + OI walls computed LIVE from the IBKR chain (OI via
tick 101 + our BS gamma), verified live 2026-08-10. Tests: `test_dual_strategy`, `test_gex`,
`test_gex_strategy`, `test_single_leg` (+ existing), all green. GEX code: `src/gex.py`,
`broker.fetch_gex_chain`, `strategy.gex_entry_signal`/`gex_invalidated`, `bot._scan_gex_entries`/
`evaluate_gex_entry`/`_gex_exit_check`. GEX exits = 50% stop · OR-reclaim invalidation · 3:55 flatten,
**NO take-profit** (`GEX_TAKE_PROFIT=0`, 2026-08-10 — a TP caps the convex single-leg tail, same as trend).
**🐛 CRITICAL BUG FOUND+FIXED 2026-08-11:** `broker.fetch_intraday_data` requested MIDPOINT for indices →
**SPX MIDPOINT returns 0 bars** (an index has no bid/ask) → trend/gex bailed at the empty-bar check
EVERY scan → **the bot never evaluated a single entry** (the ~zero trades were this, NOT the filters).
Fixed: TRADES-first (works for indices), MIDPOINT fallback, + a warn-log on empty bars. So the forward
test effectively STARTS on the next restart. Also 2026-08-11: bot now **saves the live GEX chain** every
refresh → `data/gex/chain_YYYY-MM-DD.csv` (ts, spot, gflip, strike, oi_call, oi_put, iv, T) — accumulating
our OWN historical GEX dataset (buying it is costly); + 5-min live Gflip/regime logging for diagnosis.
**🐛 2026-08-13:** the FIRST real single-leg signal (GEX 7800C @ 10:01) fired and exposed **3 execution
bugs that blocked it** — index-option tick ($0.10 at premium ≥ $3, else IBKR error 110) + two single-leg
notify crashes (None-format on `adx/vwap/orb`, missing `short_strike`). All fixed + tested
(`test_single_leg_execution`); (A)+(B) would have blocked **every** single-leg order. Also added a
**data-feed drop/restore Discord alert** (`broker._check_data_farm` → `notify_data_farm`, on-change/deduped)
after 08-12's self-healed ~18-min farm blackout, + a pandas-2.x chained-assignment cleanup in
`fetch_intraday_data`. **Forward test still has 0 clean single-leg fills — next signal (post-restart) is the
real first.** ⚠️ startup-adoption only rebuilds spreads, not lone single legs (daily 3:55 flatten mitigates).

**(2026-08-08): PIVOTED from breakout → a TREND strategy.** The breakout
premise never cleared fees (five weeks, ~−$1.8k paper; exhaustive testing — filters on/off,
follow/flip, XSP→SPX — showed no config convincingly clears fees; the signal is anti-predictive and
fees eat ~85% of any edge). The breakout code is **paused, not deleted** (`STRATEGY=breakout`
switches back). New live-paper bet: **Supertrend(7,3) + PSAR(0.02,0.2) + Kaufman-chop ≤50**, SPX,
**single-leg directional** (`TREND_LEGS=single` — buy one ATM ~50Δ CALL on an up-flip / PUT on a
down-flip), **1 contract, NO take-profit**, 50% stop / Supertrend-reversal / EOD, **drop the power
hour** (`TREND_WINDOWS=09:30-14:00` — theta kills naked longs late) and **skip quiet mornings**
(`TREND_SKIP_LOWIV=0.082`, entry-time realized vol, no lookahead). Built on a **3-year SPX backtest**
(`scripts/backtest_spread_dollars.py`, real BS legs): single-leg net **+$12,985/3yr (t≈1.3)** vs the
$10 spread's +$1,328 — the fee-halving + convex uncapped tail is where the money is. **But it's
in-sample, ~80% from 2025, still can't clear 2024, and t≈1.3 isn't significant** → a **forward
paper-test of a promising-but-unproven candidate**, not a found edge. Portable findings: **single-leg
≫ spread (half fee, uncapped tail); no-TP for single-leg (a TP caps the convex tail that IS the edge);
single-leg wants the OPEN not the power hour (theta); skip low-vol days.** ⚠️ **Known gap: startup
adoption only reconstructs spreads, not a lone single leg** — don't restart with a single-leg position
open. See `docs/GO_LIVE.md` and `docs/BACKTESTING.md`.

## Architecture (modular — refactored from one monolith)

| File | Role |
|------|------|
| `src/main.py` | Entry point: asyncio loop + logging setup, then `TradingBot().run()` |
| `src/config.py` | All `.env` config |
| `src/bot.py` | `TradingBot` — **state + orchestration + main loop only** (the conductor) |
| `src/broker.py` | `IBKRBroker` — **all** IBKR calls (connect, market data, orders, positions, permId, commissions) |
| `src/strategy.py` | **Pure functions** (no I/O): indicators, entry signal, conviction score, exit rules, condor logic — unit-testable |
| `src/notifier.py` | Discord: transport + every message template |
| `src/audit.py` | `audit.csv` writer (financials only) |
| `src/market_time.py` | ET market-hours helpers |
| `src/logging_setup.py` | Operational logging → `logs/bot.log` (daily rotation) |

Dependency flow: `bot → {broker, strategy, notifier, audit} → {config, market_time}`.
Nothing imports `bot`. Keep `strategy.py` pure. **After any code change the bot must be
restarted** (state is in-memory).

## Three record-keeping systems (don't conflate)

- **`audit.csv`** — financial ledger, one row per fill. Has a `PermId` column (IBKR's
  permanent order id) that joins each row to the IBKR account. `RECONCILE` rows are
  annotations, not trades.
- **`logs/bot.log`** — operational log (what the bot *did* + errors), daily rotation, ET.
- **`dashboard.xlsx`** — regenerated automatically after each trading day.

## Scripts

- `scripts/reconcile_ibkr.py [YYYY-MM-DD] [--write]` — reconcile audit vs IBKR **by permId**;
  flags orphans two ways; shows account `dailyPnL` (the truth). **Full guide: `docs/RECONCILE.md`.**
- `scripts/backfill_permid.py` — retro-fill `PermId` on recent audit rows (~24h window).
- `scripts/build_dashboard.py` — `audit.csv` → `dashboard.xlsx`.
- `scripts/counterfactual.py SYMBOL HH:MM` — "what did SYMBOL do after this ET time?" (retro helper).
- **Backtest/analysis tools** (`backtest_39.py`, `flip_analysis.py`, `replay_invalidation.py`,
  `validate_xsp.py SYM`) — **see `docs/BACKTESTING.md`** for what each answers, the data inputs,
  and the caveats every result is subject to (proxy P&L, fees excluded, single-symbol SPY,
  rolling-vs-session VWAP). Read it before quoting or extending any backtest.

IBKR clientIds: bot=1, reconcile=9, backfill=11 (so scripts run alongside the bot). Gateway on port 4002.

## Docs map

- `docs/HOW_IT_WORKS.md` — full bot lifecycle + config reference table.
- `docs/PLAYBOOKS.md` — entry/exit/P&L **by structure** (debit vs condor; why no lone credit spread).
- `docs/RECONCILE.md` — how to use `reconcile_ibkr.py`.
- `docs/RETROSPECTIVE.md` — **the daily journal + hypotheses under test** (append after each trading day).
- `docs/GO_LIVE.md` — paper→live readiness gates (Gate 2 = fee-adjusted profit is the blocker).
- `docs/BACKTESTING.md` — the analysis tooling, data inputs, and caveats; how every backtest number was produced.
- `TODO.md` — prioritized backlog (P0/P1/P2/P3 + a Done section).

## Strategy in one screen

**Debit entry:** `ADX>25 AND rising` + price beyond VWAP + beyond ORB level (+buffer) +
**path guard #31** (level crossed within 10 bars AND last-3-closes net-move agrees — never
fade a bounce; the 5/5 bear-trap fix) → CALL/PUT vertical. Conviction score 0–5 sizes it;
**skip if score < `MIN_CONVICTION_SCORE`(2)**. Condors DISABLED 07-10 (#28).
**Debit exits (priority):** resting take-profit at +60% · hard stop −70% · VWAP-recross
invalidation (3 bars) · trailing stop after +50% peak · EOD flatten 15:55 ET.

**Condor entry (fallback when no directional signal):** `ADX<22` + `≥8 VWAP crosses` +
11:00–13:30 ET + price mid-range → sell iron condor around the day's high/low.
**Condor exits:** buy back at 50% of credit · hard stop −70% (1.7× credit) · range-breach
(2 closes beyond a short strike) · EOD flatten. Sized by max loss.

**Shared guards:** cooldown (30m), invalidation throttle (2 *losing* invalidations/signal),
circuit breaker (5 consecutive losses), daily loss limit (−$400), 12 trades/day.

## Hard-won learnings (don't relearn these)

1. **Fees are the existential problem.** ~$100/day, charged per-contract, so *bigger*
   positions don't improve the ratio — **wider spreads (TODO #20)** and **fewer/higher-
   conviction trades** do. The edge is currently smaller than the transaction cost.
2. **Reconcile AFTER settlement, never intraday.** 0DTE marks near expiry are unreliable.
   On 07-09 a pre-settlement run showed −$124; settled truth was **−$906**. Only account
   `dailyPnL` post-settlement is real.
3. **`permId` is the join key** between `audit.csv` and IBKR.
4. **Real-time data is on** (`IBKR_MARKET_DATA_TYPE=1`). It was delayed (type 4) through
   2026-07-08 — retros before then reflect 15-min-delayed decisions.
5. **Timezone:** strategy runs in **ET** (pytz America/New_York). Audit + logs are ET now.
   The machine is **Central (CDT)** — audit rows before 2026-07-05 are in CDT.
6. **Don't restart with a condor open** — startup adoption reconstructs verticals but
   *not* condors (alerts instead).
7. **The bot runs on a laptop** — needs an always-on host before live (TODO #16); laptop
   sleep has orphaned/killed it before.
8. **Condors are a net drag so far** (1W/2L, structural 86% breakeven WR) — TODO #28 says
   consider `CONDOR_ENABLED=false` until the debit side is fee-adjusted-green. (Disabled 07-10.)
9. **Assignment is real and the bot is blind to it (07-10 → 07-13, ≈ −$9k).** SPY/QQQ/IWM
   are **American-style ETF options → assignable.** A short leg left open into expiry (a
   failed/residual close like the #30 leftover, or a condor held to the bell) expires ITM
   and becomes **stock** over the weekend. On 07-13 the account held 400 QQQ + 600 SPY
   assigned shares (≈ −$9k) that dwarfed the −$207 of spread P&L — and `audit.csv`/`bot.log`
   never saw them (ledger tracks options only). **Lesson: reconcile the IBKR *account
   positions* (stock too), not just the bot ledger; and move to cash-settled European index
   options (XSP) that can't be assigned (TODO #3, now P0).**
10. **"The guards strangle trend days" — TESTED 07-27, and it's mostly WRONG.** For two
    weeks the retros blamed the entry filters (rising-ADX gate, #31 path-freshness) and the
    VWAP-invalidation exit for the losses. A proper backtest (`scripts/backtest_39.py`,
    signal-replay over 20 days) contradicts it: un-blocking those entries makes results
    **worse** (39%→35% win, −9→−134 bp; the added trades are 31% win, net-negative — and
    bad *even with the invalidation exit off*). And the invalidation exit is net-**protective**
    (−9 with it vs −63 without), not a whipsaw-villain. The salient "we got whipsawed out of a
    winner" days (07-13, 07-20) were real but cherry-picked; in aggregate the rule and the
    guards help. **Lesson: the entry filters are NOT the bottleneck — even guard-filtered
    entries are ~39%/−9bp before fees. The problem is the breakout premise in this regime +
    fees. Don't loosen the guards.** The one real signal is the flip test (we're systematically
    on the wrong side — a regime/direction problem, not a filter one). #39 downgraded to P3.

## Bug history (the big ones, all fixed)

- **Broken exit rule** (gave back at 70% of tiny peaks, ejected at losses) → rewrote to
  hard-stop + trail-after-+50% + invalidation + +60% target.
- **#21 Orphaning (critical, cost ~$688 on 07-09):** reconciliation false-dropped *live*
  positions when `ib.positions()` returned empty → hardened: fail-open on empty feed,
  any-leg check, 180s time-based confirmation, + anti-cascade entry guard. Unit-tested.
- **#25:** no key linking audit↔IBKR → added `permId` tracking + audit column + backfill.
- **#26:** close-order reject/retry infinite loop → capped at 4 attempts w/ 30s cooldown +
  give-up alert + error-201 order sweep. Unit-tested.
- **#30 (07-10, left an inverse 6-lot residual):** a `Cancelled` close had actually FILLED;
  blind resubmit double-closed → close path now trusts fills + account position, never
  status: fills-check on dead orders, per-submission requantification, over-close halt,
  real-error-code capture. Unit-tested (`scripts/test_close_integrity.py`).
- **#31 (07-10):** 5/5-conviction bear traps (2×) — entries were path-blind (couldn't tell
  fresh breakdown from recovery hovering under the level) → `path_confirms()`: level must
  be crossed within 10 bars AND last-3-closes net-move must agree with the signal.
- **#SL-EXEC (2026-08-13, blocked the FIRST real single-leg trade):** the maiden GEX signal
  (BUY SPXW 7800C @ 10:01) never executed — THREE single-leg-path bugs: (A) `option_tick`
  returned $0.05 for SPX at all prices, but SPX options tick **$0.10 at premium ≥ $3** → limit
  11.65 bounced with **IBKR error 110**; (B) `notify_submit` crashed formatting the single-leg
  indicator dict (`adx/vwap/orb` are explicit `None` → `f"{None:.2f}"`, since `.get(k, 0)`
  returns `None` when the key is present-but-None); (C) `notify_filled` dereferenced
  `trade['short_strike']`, absent on single legs (KeyError, latent — the order never filled).
  **(A)+(B) would have blocked EVERY single-leg order (trend AND gex), not just this one.**
  Fixed: price-aware `option_tick(symbol, price)` + tick-snapped limit, `(x or 0)` coercion +
  single-leg-aware notify templates. Tested (`test_single_leg_execution`). Lesson: **index
  options tick $0.10 at premium ≥ $3 — always snap limits to the price-aware tick.**

## How we work (conventions the user expects)

- **After each trading day:** write a retro entry in `docs/RETROSPECTIVE.md`, and run
  `reconcile_ibkr.py` *after settlement* for the true P&L.
- **Every code change:** keep `README.md`, `docs/HOW_IT_WORKS.md`, and relevant docs in
  sync; move `TODO.md` items to Done with a dated note.
- **Evidence-based:** log hypotheses in RETROSPECTIVE/TODO; act on patterns across days,
  not single days. Roll out risky changes log-only first (like the breadth filter, the
  conviction size-up tier).
- **Be honest:** call out when something isn't working or when a prior conclusion was
  wrong (e.g. the −$124 → −$906 correction). The user values candor over optimism.
- **The user generally wants me to implement**, not just plan — but confirms big/risky
  or ambiguous calls. Verify with syntax checks + unit tests; the shell/Gateway are
  sometimes down (say so, hand over commands).

## Current priorities (see TODO.md for detail)

**🔄 TREND PIVOT (2026-08-08) — forward paper-test of the Supertrend strategy.** Live config
(`STRATEGY=trend`, `.env` + `src/config.py`): **SPX $10** spread, **1 contract**, entry = Supertrend
flip + PSAR agree + kauf ≤50, **only** in the 9:30–10:00 / 15:00–16:00 ET windows; exits = 50% hard
stop · Supertrend reversal · 60% resting TP · EOD flatten. No condor/conviction/trail/VWAP-invalidation
in trend mode. Code: `strategy.trend_entry_signal`/`trend_reversed`/`supertrend_dir`/`psar_dir`/
`kaufman_chop` (pure, unit-tested in `scripts/test_trend_strategy.py`), wired via `bot._scan_trend_entries`
/`evaluate_trend_entry`/`_trend_exit_check` + `market_time.in_trend_window`. **To activate: restart the
bot** (state is in-memory; it's currently running the OLD breakout code).

**The one job:** does the trend strategy clear fees LIVE on SPX? Backtest = +$1,328/3yr at $10 but
**within noise (t≈1.1), 7up/6down quarters, soft since 2025** — so read every trade; this is the real
out-of-sample check. *Don't parameter-sweep to rescue it* (fits noise). Next structural idea (not a
knob): **single-leg directional** — halves the ~$2,178 fee bill, uncaps winners; the BS engine already
prices single legs. Backtest tooling: `scripts/backtest_spread_dollars.py` (real BS legs, per-quarter,
per-trade CSV), 3-yr SPX cache `scripts/.spx_1min_2y_cache.pkl` (2023-07→2026-08, `pull_spx_2y.py`).

- **Housekeeping:** account isn't flat — paper still holds **−600 SPY / −400 QQQ** assigned-stock
  residue (learning #9); flatten before trusting the trend P&L. **#16 (always-on host)** still the one
  infra task worth doing (down-days poison the sample).

**Resolved by evidence (don't reopen):** breakout never clears fees (#39 filters aren't the bottleneck,
flip is regime-neutral #42/#37); for the trend strategy — 30% stop worse than 50%, before-1PM cutoff
was 3-month noise, $500-sizing was a power-hour mirage. See docs/BACKTESTING.md.
