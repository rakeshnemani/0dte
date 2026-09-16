# Mechanical GEX — Live-Ready Checklist

The go-live gate **specifically for the mechanical `gex:SPX` sleeve** (not thesis, not trend).
Live money is switched on only when **every gate passes** — not when a good week feels convincing.
This is the philosophy of [GO_LIVE.md](GO_LIVE.md), scoped to one strategy and re-based to a frozen ruleset.

> **📊 Live scoreboard: `python scripts/go_live_status.py`** — per-eval-trade net-P&L ledger and the current
> pass/fail of every numeric gate below. Run it any time to see how close we are.

> **Day 0 = 2026-09-13** (reset from 09-10 → 09-05 as robustness fixes landed: IntoWall skip on 09-10, then the
> regime-known gate + widened chain on 09-13 — each ruleset change resets the clock). The evaluation clock counts
> **from the next market session forward**. Pre-Day-0 trades are *history* and do **not** count (the only one,
> the 09-10 −$880 IntoWall loss, motivated these very fixes). *(These are robustness/safety fixes — they make the
> regime reliable and stop trading blind — not a change to the edge thesis; the clock resets to stay rigorous.)*

## The frozen ruleset under evaluation (as of Day 0)

Changing any of these **resets the clock** (a go-live sample must test a *frozen* system):

- **Entry:** neg-gamma OR wall-breakout · **regime-known gate (2026-09-13: skip if `gamma_flip`=None)** · 15-min
  OR breakout · 2-bar momentum · low-vol skip (≥0.082) · **exhaustion gate `Range_Exp_Ratio` < 0.8** ·
  **IntoWall skip** (2026-09-10, `GEX_SKIP_INTOWALL` — skip a setup buying into the nearest heavy wall).
- **Exit:** trailing (arm +35%, **tiered** giveback 60/35/20% by peak band) · **−60% catastrophe** · 15:55 EOD flatten. No fixed stop, no TP.
- **Chain fetch:** `GEX_CHAIN_MAX_STRIKES=100` (widened 50→100 on 2026-09-13 — the ±1.6% window let `gamma_flip`
  return None on trend days → `Regime=unknown`, which let 09-10's wall-breakout fire blind; ~full ±5% now captures the flip).
- Shared guards: cooldown 30m · circuit breaker 5 · daily loss −$400 · 12 trades/day · anti-cascade.

## Pre-Day-0 baseline (the honest starting line)

Mechanical GEX to date: **11 trades, 5W / 6L (45%), −$615, PF < 1.** The book is +$310 only because the
**thesis** sleeve (+$925) carries it. The known weak spot: **GEX PUTs are 2W/5L, −$1,920** (the "PUT leak");
GEX CALLs are 3W/1L, +$1,305. **Mech GEX is not close to any gate below — that is the point of having them.**

---

## Gate A — Performance (fee-adjusted, reconciled) — THE REAL BAR

All numbers are **realized, net of commissions (~$3.26/round-trip), reconciled AFTER settlement** — never
intraday 0DTE marks (they lie: the −$124 → −$906 correction, learning #2).

- [ ] **Cumulative fee-adjusted P&L ≥ +$10,000** *(user gate #1)* — mech-GEX sleeve only, from Day 0.
- [ ] **Profit factor ≥ 1.5** after fees *(the load-bearing metric — see note)*.
- [ ] **Win rate ≥ 65%** *(user gate #2 — see the analyst note below)*.
- [ ] **Positive net expectancy per trade** after fees.
- [ ] **Max peak-to-trough drawdown ≤ ~$2,500** during the eval (no catastrophic losing streak).

> **Analyst note on win rate vs the convex tail.** Mech GEX is a **no-take-profit, single-leg convex** design:
> by construction it expects *many small losers paid for by a few big winners*, which is usually a **sub-50%
> win-rate** shape (that's the edge, not a flaw). A **65% WR bar is very demanding and can conflict** with
> letting winners run / losers hit the catastrophe stop. **Recommendation: treat profit factor + expectancy as
> the primary gates and 65% WR as aspirational.** If you want, we soften WR to ≥50% and lean on PF ≥ 1.5 — your
> call; I've kept 65% as you specified.

## Gate B — Sample size & frozen ruleset

- [ ] **≥ 40 mech-GEX closed trades** from Day 0 (at ~2–3/week this is *many* weeks — the honest timeline).
- [ ] **≥ 30 trading days** of continuous operation from Day 0.
- [ ] **No ruleset changes in the eval** — any entry/exit/param change resets this clock.

## Gate C — Regime coverage *(user gate #3, made concrete)*

The strategy must handle every market type — where "handle" includes **correctly NOT trading** a day with no edge.

- [ ] **Bullish / trend-up day** — either a neg-gamma up-move captured, **or** correctly no-trade.
      *(Known gap: mech GEX is blind to positive-gamma trend-up days — 09-03 missed a +1% run. Either the
      positive-gamma trend-long path is built, or this is documented as an accepted no-trade.)*
- [ ] **Bearish / trend-down day** — a **PUT winner captured cleanly** *(stress-tests the current leak — the
      weak side must be shown to work, not just avoided).*
- [ ] **Choppy / low-vol day** — damage minimized or correctly stayed out *(09-04 was a clean example: low-vol +
      exhaustion gates kept it flat).*
- [ ] **High-volatility event day** (CPI / FOMC / jobs) — survives without catastrophic loss; ideally guards
      keep it small or out.
- [ ] **Each regime seen ≥ 3 times** — not one lucky instance.

## Gate D — Edge / filter validation

- [ ] **Exhaustion gate (0.8) shown net-helpful out-of-sample** — it was *derived* from the 08-25/08-28 losers
      (in-sample); confirm it helps (or at least doesn't hurt) on days it wasn't fit to.
- [x] **IntoWall watch resolved (2026-09-10) → promoted to a live skip** (`GEX_SKIP_INTOWALL`). Evidence: 0-3,
      −$2,795 (08-18/08-19/09-10) — every mechanical IntoWall trade lost. Now watch forward that the skip only
      removes losers (no IntoWall winner it would have blocked).
- [ ] **The PUT leak diagnosed** — understand *why* mech-GEX PUTs lose (2W/5L) before trusting live capital on
      the short side; either the entries measurably improve, or shorts are gated.

## Gate E — System & execution reliability

- [ ] **Every exit path executed on a REAL fill** (not just unit tests): tiered trailing stop · −60% catastrophe
      · 15:55 EOD flatten — each observed closing an actual trade *(the #SL-CLOSE bug once made a live 0DTE long
      un-closeable all day).*
- [ ] **Guards observed firing**: daily-loss limit, circuit breaker (forced test OK), anti-cascade untracked-guard.
- [ ] **Always-on host** — bot runs unattended through the full session; **≥ 20 consecutive clean sessions**,
      zero manual intervention, zero unmanaged positions *(TODO #16 — the laptop-sleep orphan risk, learnings #6/#7).*
- [ ] **Reconciliation clean** for the whole eval — audit ↔ IBKR by `permId`, post-settlement, no orphans.

## Gate F — Live-transition mechanics *(only after A–E pass)*

- [ ] **Risk budget written** — live size, max risk/trade, real-dollar daily loss cap, one-command kill switch.
- [ ] **Live ramp plan** — start at **1 contract**, 2 weeks; compare live fills vs paper mids (abort if slippage
      > $0.03/leg); scale on evidence only.
- [ ] **Dead-man's-switch monitoring** — alert if the bot goes silent during market hours (the hourly health ping
      is the seed of this).

---

## Progress

```
Gate A  Performance         ░░░░░░░░░░  far   (−$615 sleeve, PF<1, 45% WR — target +$10k / PF 1.5 / 65%)
Gate B  Sample & frozen      ░░░░░░░░░░  Day 0 reset 2026-09-13 (0 of 40 trades, 0 of 30 days)
Gate C  Regime coverage      ██░░░░░░░░  chop ✓ (09-04); bull/bear/event pending
Gate D  Edge validation      ██░░░░░░░░  IntoWall skip live ✓; exhaustion in-sample; PUT leak open
Gate E  System reliability   ███░░░░░░░  features built; always-on host + 20-clean-sessions pending
Gate F  Live mechanics       ░░░░░░░░░░  not started (by design)
```

**Honest bottom line (Day 0):** mech GEX is currently the *weaker* of the three sleeves and net-negative on its
own. These gates are deliberately demanding — clearing them is months out and is **not guaranteed** (the data so
far suggests the neg-gamma-momentum premise and the PUT side are the problem). Re-assess against this list each
Friday; move a bar only on evidence.
