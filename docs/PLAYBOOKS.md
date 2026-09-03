# Playbooks — entry / exit / P&L per strategy

> **2026-08-17:** the bot is **single-leg only** — one contract. Buy ONE ATM (~50Δ) option — CALL
> bullish, PUT bearish — for whichever strategy fires (the multi-leg machinery was deleted). Two
> strategies run together (`STRATEGY=trend,gex`), each holding its own SPX
> position keyed `strategy:symbol`. Both are **1 contract, NO take-profit** (the convex tail is
> the edge — a TP caps exactly the winner that pays for the losers).

## Why single-leg (and why no take-profit)

- **Half the fee.** One leg, not two — the per-contract fee is the existential drag on 0DTE,
  so halving the leg count directly improves the fee-adjusted edge.
- **Uncapped convex tail.** A long option's payoff is convex; the rare big winner is where the
  money is. A second (short) leg would cap it, and a take-profit caps it (early exit) — both throw
  away the edge. So: single leg, no TP.
- **The cost:** a long single leg decays (theta) and can lose 100% (full premium) on a bad day.
  That's the risk we accept for the tail — mitigated by the low-vol skip (don't pay theta on a
  dead tape) and, per strategy, a stop or a catastrophe backstop.

## Trend strategy

**Entry** (inside a `TREND_WINDOWS` slot, default 09:30–14:00 ET — the power hour is dropped
because theta kills naked longs late):
- Supertrend(7,3) **flips** into a direction, AND
- PSAR(0.02,0.2) **agrees** with that direction, AND
- Kaufman chop ≤ `TREND_KAUF_MAX` (50) — a clean trend, not a choppy reversal, AND
- entry-time realized vol ≥ `TREND_SKIP_LOWIV` (0.082) — the tape is actually moving.

→ Buy 1 ATM CALL (up-flip) / PUT (down-flip).

**Exits** (priority):
1. **Hard stop** −`HARD_STOP_LOSS_PCT` (50%).
2. **Supertrend reversal** — Supertrend flips against the position (thesis dead).
3. **EOD flatten** 15:55 ET — never hold 0DTE into the close.

No take-profit, no trailing, no VWAP-invalidation.

## GEX strategy (forward-test only — no historical GEX data)

**Entry** (inside `GEX_WINDOWS`, default 09:30–15:55 ET; Gflip + concentration walls computed
**live** from the IBKR option chain):
- **Regime:** spot < Gflip (negative gamma — dealers amplify moves) **OR** price breaking through
  an OI concentration wall, AND
- **Opening-range breakout:** 1-min close beyond the 15-min opening range (`GEX_OR_MINUTES`), AND
- **Momentum:** the last `GEX_MOMENTUM_BARS` (2) closes accelerate in the breakout direction, AND
- entry-time realized vol ≥ `GEX_SKIP_LOWIV` (0.082), AND
- **Exhaustion (2026-08-31):** `Range_Exp_Ratio` < `GEX_RANGE_EXP_MAX` (0.8) — skip once the day has
  already realized ≥ 80% of its IV-expected move (the move is largely spent → chop ahead). Mechanical
  GEX only; thesis trades are human-authorised and ungated.

→ Buy 1 ATM CALL (bullish break) / PUT (bearish break).

**Exits — LET THE CONVEX TAIL RIDE** (2026-08-17; there is **no** invalidation cut and **no**
fixed max-loss stop — those cut the 08-17 winner at −4% before it ran to +100%):
1. **Trailing stop** — arms once the trade peaks at `GEX_TRAIL_TRIGGER` (+35%), then exits if it
   gives back a **tiered** fraction of the peak: 60% in [35–50%), 35% in [50–70%), 20% at 70%+
   (`GEX_TRAIL_GIVEBACK_LOW/MID/HIGH`). E.g. peak +40% → exit +16%; peak +100% → exit +80%.
2. **Catastrophe backstop** −`GEX_CATASTROPHE_STOP` (60%) — the only downside floor, since the
   trail can't arm on a trade that never gets into profit.
3. **EOD flatten** 15:55 ET.

⚠️ A GEX trade that goes straight against us has **no protection until −60%** (~−$520/contract).
That's the accepted cost of not cutting winners early. GEX has no backtest — watch it live.

## P&L / order mechanics (both strategies)

- **Entry limit** = mid + `ENTRY_AGGRESSION`·(ask − mid), snapped to the price-aware option tick
  (SPX ticks $0.05 below $3.00 premium, **$0.10 at/above** — an off-tick limit is rejected with
  IBKR error 110; see `broker.snap_to_tick`).
- **Entry timeout** — an unfilled entry is cancelled after `ENTRY_ORDER_TIMEOUT_SECONDS` (120s)
  and the signal is re-evaluated fresh (a resting limit only fills once the market moved against
  the thesis).
- **Close** = the current option mid, snapped to the tick, SELL 1 contract; repriced if it sits
  unfilled; capped at 4 attempts then a "close manually" alert.
- **P&L** = (exit − entry) × 100 per contract, gross; commissions (~$1.63/side on SPX) are a
  separate audit column. Booked from the **actual fill**, not the submission price.

## Shared account guards

Cooldown (30 min per symbol×direction), circuit breaker (5 consecutive losses), daily loss limit
(−$400 realized), 12 trades/day, and an anti-cascade guard (never open on top of an untracked
account position). Guards are shared across both strategies; positions and cooldowns are per-strategy.
