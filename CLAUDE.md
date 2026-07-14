# CLAUDE.md — 0DTE Trading Bot

Orientation for working on this repo. Read the linked docs for depth; this file is the map.

## What this is

A Python **0DTE options-spread trading bot** on **Interactive Brokers paper trading**
(via `ib_insync`). Trades **SPY, QQQ, IWM**. Two regime-matched structures:
- **Directional debit spreads** (CALL/PUT verticals) on trend days.
- **Iron condors** (sell premium) on range/chop days.

**Goal:** reach consistent, *fee-adjusted* profitability on paper, then go live.
**Status (2026-07-14): NOT profitable, but now running on XSP-only.** Thin-edge coin-flip
(~45% WR, ~break-even gross, net-negative on ~$100/day commissions). **#3 done + validated
live 07-14** — migrated to **XSP-only** (cash-settled European index options → *no
assignment*, unlike the 07-10 SPY/QQQ debacle); first live XSP trade executed clean on
CBOE. Still losing on the **entry-quality / VWAP-whipsaw** problem (#32 buffer + #33
anchored VWAP are the open levers). See `docs/GO_LIVE.md` (~20% ready).

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

IBKR clientIds: bot=1, reconcile=9, backfill=11 (so scripts run alongside the bot). Gateway on port 4002.

## Docs map

- `docs/HOW_IT_WORKS.md` — full bot lifecycle + config reference table.
- `docs/PLAYBOOKS.md` — entry/exit/P&L **by structure** (debit vs condor; why no lone credit spread).
- `docs/RECONCILE.md` — how to use `reconcile_ibkr.py`.
- `docs/RETROSPECTIVE.md` — **the daily journal + hypotheses under test** (append after each trading day).
- `docs/GO_LIVE.md` — paper→live readiness gates (Gate 2 = fee-adjusted profit is the blocker).
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
10. **The risk/filter layer is over-fit to chop and strangles trend days (07-13).** The
    guards we added over two weeks to survive chop (rising-ADX entry gate, VWAP-recross
    invalidation exit) turned a −2% *down day where our PUT direction was right* into a
    loss: invalidation whipsawed us out of SPY/IWM before the move paid, and the ADX-slope
    gate blocked every QQQ entry on QQQ's biggest down day. Fix = **regime-aware** exit/gate
    (TODO #32), not a rewrite — the direction engine was right.

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

**▶️ RUNNING on XSP-only (2026-07-14).** #3 landed + validated live (clean CBOE fill, no
assignment). Reminder: to pause a *running* bot you must **stop the process** — a `.env`
edit won't affect the live process (config is in memory).

- **P0 — active:** **#33** anchored/session VWAP (rolling-14 hugs price → 32/33 entries
  invalidate; the real edge lever) · **#32** regime-aware exit — mechanisms built + default-
  off; replay says `VWAP_INVALIDATION_BUFFER_PCT=0.0005` is the one small win (hold/override
  showed nothing). **#3 XSP migration: DONE (validated live 07-14).**
- **P1:** #2 total-exposure cap · #16 always-on host · #22/#28 condor tuning.
- **P2 (fee viability):** #20 wider spreads to cut the fee ratio; #5 time stop, #6 midday
  tightening, #7 expected-move anchor, #12 2-hour throttle.
- **P3:** #4 GLD/TLT, #8 VIX1D.

**Two open questions now:** (1) can the debit strategy clear its fees? (~break-even gross;
#20 is the lever). (2) Does making the exit/gate **regime-aware** (#32) recover the
trend days we're currently strangling? 07-13 says the direction engine is right — the
scaffolding around it is what's losing. XSP migration (#3) is the mandatory safety gate
before any of that matters.
