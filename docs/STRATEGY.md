# Strategy — 0DTE Single-Leg Trend + GEX

A detailed reference for **what** the bot trades, **why**, and the **exact** entry and exit criteria.
For the code lifecycle see [HOW_IT_WORKS.md](HOW_IT_WORKS.md); for a one-screen summary see
[PLAYBOOKS.md](PLAYBOOKS.md); for the config knobs see [`src/config.py`](../src/config.py).

---

## 1. Overview & core philosophy

The bot runs **two independent directional strategies at once** (`STRATEGY=trend,gex`), each holding
its own SPX position (trades keyed `strategy:symbol`, e.g. `trend:SPX` and `gex:SPX`). Both do the
same *kind* of trade and differ only in the **signal** and the **exit rules**:

> **Buy ONE at-the-money (~50Δ) 0DTE option — a CALL when bullish, a PUT when bearish — 1 contract,
> held as a naked long, with (by default) no take-profit.**

Three design beliefs drive everything:

1. **Fees are the existential problem on 0DTE.** IBKR charges per contract per leg (~$1.63/side on
   SPX). A single leg is **half the leg count** of a two-leg structure, which directly halves the fee
   drag — the single biggest lever we have.
2. **The edge is the convex tail.** A long option's payoff is convex: most trades are small, but the
   rare big winner pays for many small losers. A short leg caps that tail; so does a take-profit.
   Both throw away the edge — so we run a **single leg with no take-profit**.
3. **Theta is the price of the tail.** A naked long decays every minute and can lose 100% (the full
   premium) on a bad day. We accept that in exchange for the tail, and mitigate it two ways: a
   **low-volatility skip** (don't pay theta on a dead tape) and a per-strategy stop/backstop.

**Instrument:** SPX index options (root **SPXW**), 0DTE, ATM. SPX is **cash-settled and
European-style → no assignment risk** and no overnight stock. 1 contract = 100× the index (~$1.6M
notional at 16,000), so one contract is meaningful size with the tightest possible fee ratio.

---

## 2. Building blocks (indicators & concepts)

These are computed on the underlying's **1-minute bars** (fetched live from IBKR, `whatToShow=TRADES`).

### Trend indicators
| Indicator | Definition (as coded) | Read |
|---|---|---|
| **Supertrend(7, 3)** | Bands at `hl2 ± 3·ATR(7)`; direction flips to +1 when close crosses above the trailing upper band, −1 below the lower band | +1 = uptrend, −1 = downtrend |
| **PSAR(0.02, 0.2)** | Parabolic SAR (step 0.02, max 0.2) | +1 when price is above the SAR (bullish), −1 below |
| **Kaufman chop(14)** | `100·(1 − ER)`, where **Efficiency Ratio** = \|net move over 14 bars\| ÷ Σ\|bar-to-bar move\| | **0 = clean trend**, 100 = pure chop |
| **Entry-time realized vol** | Annualized std of open→now 1-min log returns (`std × √(252·390)`), **no lookahead** | The tape's realized volatility *so far today* |

### GEX (dealer Gamma Exposure) concepts
Computed **live** from the IBKR option chain (open interest + IV per strike), in [`src/gex.py`](../src/gex.py):

- **Net dealer GEX at spot S:** `Σ_strikes (OI_call − OI_put) · Γ(S,K,T,IV) · S² · 0.01 · 100`, using
  the common dealer convention (dealers **long call gamma, short put gamma**). `Γ` is Black-Scholes gamma.
- **Gflip (gamma-flip level):** the spot price where net GEX crosses zero, scanned over ±8% of spot and
  taking the crossing **nearest the current spot** (the relevant regime boundary).
  - **Spot < Gflip → negative gamma:** dealers hedge *with* the move → **amplify** → momentum-friendly
    (breakouts follow through). **This is the regime the strategy wants.**
  - **Spot > Gflip → positive gamma:** dealers *fade* the move → dampen → chop / mean-revert.
- **Concentration zones ("walls"):** the top-3 strikes by call-OI and by put-OI. These heavy-OI strikes
  are where price tends to pin/chop; a clean break *through* one is a momentum signal.

> **Honest GEX caveats:** the dealer sign is an *assumption*, not observed positioning; open interest is
> a once-a-day settled number (no intraday OI); and Γ per strike uses IBKR's live per-strike IV (or an
> estimate). GEX has **no backtest** — it is forward-tested only.

---

## 3. Strategy 1 — Trend

**Thesis:** on a genuine intraday trend, a fresh Supertrend flip confirmed by PSAR, with a low chop
reading, marks the start of a directional move an ATM option can ride. Filter out choppy reversals and
dead tapes, take one leg in the direction of the flip, and hold until the trend stop, a reversal, or the
close.

### Entry — ALL must hold
The trend scan runs only inside a `TREND_WINDOWS` slot, on symbols with no open `trend` position, and
needs ≥ `TREND_MIN_BARS` (20) bars of warmup:

1. **Supertrend flips into a direction on the latest bar** — the direction must *change* this bar
   (`cur ≠ prev`, `cur ≠ 0`), not merely persist. Up-flip → CALL, down-flip → PUT.
2. **PSAR agrees** — PSAR direction on the latest bar equals the Supertrend direction.
3. **Kaufman chop ≤ `TREND_KAUF_MAX` (50)** — a clean trend, not a choppy reversal. *(A flip that passes
   1–2 but fails this fires a "⏸️ setup skipped" Discord alert for transparency.)*
4. **Entry-time realized vol ≥ `TREND_SKIP_LOWIV` (0.082)** — the tape is actually moving; on a
   dead-quiet morning a naked long just bleeds theta, so we skip.
5. **Inside a `TREND_WINDOWS` slot** — default `09:30-14:00` ET. The power hour is dropped: theta
   accelerates into the close and kills a naked long that hasn't already worked.

→ **Buy 1 ATM CALL** (up-flip) or **PUT** (down-flip).

### Exit — first to trigger wins
1. **Hard stop** — the option has lost **`HARD_STOP_LOSS_PCT` (50%)** of its entry value.
2. **Supertrend reversal** — Supertrend flips *against* the position (CALL and Supertrend turns −1, or
   PUT and it turns +1). The thesis is dead; get out.
3. **EOD flatten** — force-close at `EOD_FLATTEN_TIME` (15:55 ET). Never hold a 0DTE long into the close.

**No take-profit, no trailing stop, no invalidation** — the winner is left to run to the reversal or the
bell.

### Trend parameters
| Param | Default | Meaning |
|---|---|---|
| `TREND_SUPERTREND_PERIOD` / `_MULT` | 7 / 3.0 | Supertrend ATR window / band multiplier |
| `TREND_KAUF_N` / `TREND_KAUF_MAX` | 14 / 50 | Kaufman window / max chop to allow an entry |
| `TREND_WINDOWS` | `09:30-14:00`¹ | ET slots the trend may enter in |
| `TREND_SKIP_LOWIV` | 0.082 | Skip below this entry-time realized vol |
| `TREND_MIN_BARS` | 20 | Warmup bars before signalling |
| `HARD_STOP_LOSS_PCT` | 0.50 | Trend hard stop |

¹ The live `.env` currently runs `09:30-15:55`; the default/backtested value is `09:30-14:00`.

---

## 4. Strategy 2 — GEX (dealer gamma-flip momentum)

**Thesis:** when spot is below the gamma-flip level (**negative gamma**), dealer hedging *amplifies*
moves, so a breakout is likely to follow through rather than fade. Combine that regime with a real
opening-range breakout and short-term momentum, and buy the direction of the break. It's a momentum
strategy that only fires in the regime where momentum actually works. *(Forward-test only — no backtest.)*

### The live GEX pipeline
Every `GEX_REFRESH_MIN` (30 min) the bot fetches the OI+IV chain from IBKR — strikes within ±5% of spot
(`GEX_CHAIN_STRIKE_PCT`), the nearest 3 expiries (`GEX_CHAIN_EXPIRIES`), capped at 50 strikes
(`GEX_CHAIN_MAX_STRIKES`) — computes **Gflip** and the **walls** at the current spot, and saves the
snapshot to `data/gex/chain_YYYY-MM-DD.csv` (building our own historical GEX dataset). The regime is
re-computed from the cached chain as spot moves and logged every ~5 min.

### Entry — ALL THREE conditions must hold at once
The GEX scan runs only inside a `GEX_WINDOWS` slot, on symbols with no open `gex` position, and needs
≥ 18 bars:

1. **Regime** — **spot < Gflip** (negative gamma) **OR** a **wall breakout**: price was at an OI
   concentration wall within the last ~5 bars and has now cleared it in the trade direction (tolerance
   `GEX_WALL_TOL_PCT`, ~0.15% of spot). *(A breakout that forms in positive gamma fires a skip alert:
   "dealers dampen, breakouts fade".)*
2. **Opening-range breakout** — the latest 1-min close is beyond the **15-minute opening range**
   (`GEX_OR_MINUTES`): above the OR high for a CALL, below the OR low for a PUT (using the more
   significant of the OR level and the prior-session H/L when available).
3. **Momentum** — the last `GEX_MOMENTUM_BARS` (2) bar-to-bar closes all accelerate in the trade
   direction (the computable reading of "delta expansion"). *(An OR break in negative gamma with no
   momentum fires a "no momentum" skip alert.)*

Plus the shared gate: **entry-time realized vol ≥ `GEX_SKIP_LOWIV` (0.082)** (theta protection).

→ **Buy 1 ATM CALL** (bullish break) or **PUT** (bearish break).

### Exit — LET THE CONVEX TAIL RIDE
This is the defining choice (adopted 2026-08-17): **no invalidation cut and no fixed max-loss stop** —
those cut a winner at −4% on 08-17 before it ran to +100%. GEX exits *only* via:

1. **Trailing stop** — arms **once the trade peaks at `GEX_TRAIL_TRIGGER` (+35%)**, then exits if profit
   gives back a **TIERED fraction of the peak** (2026-08-27, `strategy.trailing_giveback`): the giveback
   shrinks as the peak grows — loose on a small gain so a runner isn't choked, tight once it's a big winner.
   Bands: **[35–50%) → 60% (floor peak×0.40)** · **[50–70%) → 35% (peak×0.65)** · **70%+ → 20% (peak×0.80)**.
   So peak +40% → exit +16%; peak +60% → exit +39%; peak +100% → exit +80%. (Replaced the old flat 20%,
   which cut trend-day runners — 08-27 both CALLs peaked +36/44% and trailed out +28/34% while SPX ran on.)
2. **Catastrophe backstop** — exit at **−`GEX_CATASTROPHE_STOP` (80%)**. This is the *only* downside
   floor, because the trailing stop can't arm on a trade that never gets into profit.
3. **EOD flatten** — 15:55 ET.

> ⚠️ **Stated risk:** a GEX trade that goes straight against us from the open has **no protection until
> −80%** (~−$690/contract on a typical premium). That is the accepted cost of never cutting a winner
> early. Watch it live.

### GEX parameters
| Param | Default | Meaning |
|---|---|---|
| `GEX_WINDOWS` | `09:30-15:55` | ET slots GEX may enter in |
| `GEX_OR_MINUTES` | 15 | Opening-range length |
| `GEX_MOMENTUM_BARS` | 2 | Accelerating bars required |
| `GEX_WALL_TOL_PCT` | 0.0015 | "at a wall" tolerance (~0.15% of spot) |
| `GEX_SKIP_LOWIV` | 0.082 | Theta-protection vol gate |
| `GEX_TRAIL_TRIGGER` / `_GIVEBACK` | 0.35 / 0.20 | Trailing stop: arm level / giveback of peak |
| `GEX_CATASTROPHE_STOP` | 0.80 | Wide downside backstop |
| `GEX_TAKE_PROFIT` | 0.0 (off) | Optional hard TP (>0 re-enables) |
| `GEX_CHAIN_STRIKE_PCT` / `_EXPIRIES` / `_MAX_STRIKES` | 0.05 / 3 / 50 | Chain fetch scope |
| `GEX_REFRESH_MIN` | 30 | How often to re-fetch the OI chain |

---

## 5. Order execution (both strategies)

1. **Strike:** the ATM strike = underlying price rounded to `STRIKE_STEP` (SPX lists 5-point strikes).
2. **Entry limit** = `mid + ENTRY_AGGRESSION·(ask − mid)` (0.5 → halfway to the ask), then **snapped to
   the valid option tick** via `broker.snap_to_tick`. SPX options tick **$0.05 below $3.00 premium and
   $0.10 at/above** — an off-tick limit is rejected with IBKR error 110 (this once blocked every order).
   Skip the trade entirely if the mid is below `MIN_OPTION_COST` (0.30) — a fee floor.
3. **Fill discipline:** the trade is tracked `PENDING_ENTRY`. If it doesn't fill within
   `ENTRY_ORDER_TIMEOUT_SECONDS` (120s) it is **cancelled and the signal re-evaluated fresh** — a resting
   limit only fills once the market has moved *against* the thesis (adverse selection). The cooldown and
   daily trade count are set on the **fill**, not submission, so a timed-out entry costs nothing.
4. **Close:** SELL the option at the current mid, **tick-snapped**; repriced if it sits unfilled; capped
   at 4 attempts, then a "close manually" alert. P&L is booked from the **actual fill** + IBKR
   commissions, never the submission price.
5. **P&L** = `(exit − entry) × 100` per contract, gross; commissions are a separate audit column.

---

## 6. Shared risk guards

Applied in `execute_trade` before any order (positions and cooldowns are per-strategy; these account
guards are shared):

| Guard | Rule |
|---|---|
| **Signal cooldown** | A (strategy, symbol, direction) cannot re-fire for `SIGNAL_COOLDOWN_MINUTES` (30) after a fill |
| **Circuit breaker** | After `MAX_CONSECUTIVE_LOSSES` (5) consecutive losing trades → no new entries for the day |
| **Daily loss limit** | Once realized net P&L ≤ −`MAX_DAILY_LOSS` → no new entries (open trades still managed) |
| **Daily trade cap** | `MAX_TRADES_PER_DAY` (12) across all strategies |
| **Anti-cascade** | Never open on top of an *untracked* option position in the symbol (alert once) |
| **Low-vol skip** | Both strategies skip when entry-time realized vol < their `*_SKIP_LOWIV` |
| **Fast exit polling** | The loop tightens from 60s → `FAST_POLL_SECONDS` (15) when an exit needs watching (armed at +`FAST_POLL_ARM_PCT` 35% profit or a closing order in flight) |
| **Data-farm alert** | On IBKR feed drop/restore, an on-change Discord alert fires — a dropped feed blinds the bot (no bars → no entries) |

---

## 7. Design rationale & known limitations

**Why single-leg, no take-profit** — the two are the same bet: keep the fee low and keep the convex tail.
A second (short) leg and a take-profit both cap the upside that *is* the edge. Backtest (trend, in-sample):
single-leg net **+$12,985/3yr** vs the old two-leg structure's +$1,328 — the fee-halving + uncapped tail is the gap.

**Why the vol gate** — a naked 0DTE long is a bet that the move outruns theta. On a dead-quiet tape it
can't, and you just pay decay. The entry-time realized-vol floor keeps us out of those days.

**Why GEX "lets the tail ride"** — GEX is a momentum strategy; its whole premise is the occasional trade
that runs far. An invalidation cut or a 50% stop guarantees you miss those. The trailing stop protects a
*realized* winner; the wide −80% backstop caps a disaster; between them the trade is free to run.

**What is NOT proven:**
- **Trend** is a promising-but-unproven candidate: the +$12,985 backtest is **in-sample, ~80% from 2025,
  can't clear 2024, and t≈1.3 is not statistically significant.** It is a forward paper-test, not a found edge.
- **GEX has no backtest at all** (no free historical dealer-gamma data) — it is pure forward-test, and
  the dealer-sign assumption + once-daily OI are real modelling limits.
- **The single-leg downside** is the full premium on a bad day. Trend caps it at −50%; GEX only at −80%.
- The bot runs on a laptop and is data-blind during any IBKR feed outage — an always-on host is a
  pre-live requirement.

---

## 8. Full parameter reference

All knobs live in `.env` (defaults in [`src/config.py`](../src/config.py)). The exit philosophy per
strategy is the important asymmetry: **trend cuts losers at −50% and keeps no winner cap; GEX cuts nothing
early, trails realized winners, and only backstops at −80%.**

| Setting | Default | Strategy | Role |
|---|---|---|---|
| `STRATEGY` | `trend,gex` | both | Which strategies run |
| `SYMBOLS` | `SPX` | both | Underlying (cash-settled, no assignment) |
| `TREND_WINDOWS` / `GEX_WINDOWS` | `09:30-14:00` / `09:30-15:55` | — | Entry time slots |
| `TREND_KAUF_MAX` | 50 | trend | Max chop for a trend entry |
| `TREND_SKIP_LOWIV` / `GEX_SKIP_LOWIV` | 0.082 | both | Low-vol skip floor |
| `HARD_STOP_LOSS_PCT` | 0.50 | trend | Trend hard stop |
| `GEX_TRAIL_TRIGGER` / `GEX_TRAIL_GIVEBACK_LOW/MID/HIGH` | 0.35 / 0.60·0.35·0.20 | gex | Trailing stop (arm / tiered giveback by peak band, edges `GEX_TRAIL_BAND_MID/HIGH` 0.50/0.70) |
| `GEX_CATASTROPHE_STOP` | 0.80 | gex | Wide backstop |
| `MIN_OPTION_COST` | 0.30 | both | Skip if the option mid is below this |
| `ENTRY_AGGRESSION` | 0.5 | both | Entry limit mid→ask fraction |
| `ENTRY_ORDER_TIMEOUT_SECONDS` | 120 | both | Cancel an unfilled entry after this |
| `MAX_TRADES_PER_DAY` / `SIGNAL_COOLDOWN_MINUTES` | 12 / 30 | both | Trade cap / per-signal cooldown |
| `MAX_CONSECUTIVE_LOSSES` / `MAX_DAILY_LOSS` | 5 / 800 | both | Circuit breaker / daily loss limit |
| `EOD_FLATTEN_TIME` / `GEX_FLATTEN_TIME` | 15:55 | both | Force-close all positions |
