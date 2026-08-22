# Trading Retrospective Log

A running journal of daily observations from paper trading. The goal is to
**act on patterns, not single days** — we record what we see and only change the
strategy once there are enough data points across different market regimes
(trend days, chop days, reversals). Newest entries on top.

Companion to [TODO.md](../TODO.md) (strategy ideas) — this doc holds evidence and
hypotheses; TODO holds concrete build items.

---

## Backlog / Hypotheses to Validate

Candidate improvements surfaced during retros. Each notes the evidence so far and
what would confirm or kill it.

**Status update 2026-07-05 — implemented after the 07-01 chop day:**
- **#1** startup adoption (`adopt_orphan_positions()` — orphans are reconstructed and managed)
- **#4** hygiene: `Profit_Pct` ×100 fixed (historical rows rescaled), audit now logs in ET, new `ADX_Slope`/`Peak_Pct` columns, stale-bar detector (warns if last bar > 10 min old)
- **#5** thesis-invalidation exit (`VWAP_INVALIDATION_BARS=3`, active — Rule 2)
- **#6** entry chop guards (`ADX_SLOPE_BARS=10` rising-ADX gate + `ORB_BREAKOUT_BUFFER_PCT=0.1%`)

**Status update 2026-07-06 (evening) — implemented after the 07-06 chop day:**
- **#7** invalidation-aware entry throttle (`MAX_INVALIDATIONS_PER_SIGNAL=2`)
- **Conviction-based sizing** (score 0–5 → 0.5×/1×/1.5× budget; logged to audit
  `Conviction` column, shown in Discord, charted in the dashboard)

Still open: **#2** (needs regime-flip data) and **#3** (win-side tuning — re-evaluate
after the new loss-side rules have data). The new guards themselves are now
hypotheses under test: watch for **entries the slope gate blocks that would have won**,
**invalidation exits that would have recovered**, and — new — **whether the conviction
score separates winners from losers** (calibration pass after ~2 weeks of the
`Conviction` audit column).

**Experiment live (2026-07-10): `VWAP_INVALIDATION_BARS` 3 → 6.** User thesis: 3 bars
is hair-triggered because entries are born just beyond VWAP. Validated by replay
(`scripts/replay_invalidation.py`, 31 entries, underlying-proxy): **N=6 −115bp vs
N=3 −151bp** — and a full sweep shows a clean optimum at 6, not "longer is better":
N=3 −151 · **N=6 −115** · N=10 −144 · N=15 −206 · N=20 −190. Too little patience
whipsaws winners; too much lets dead trades bleed toward the hard stop. ~6 minutes
is roughly how long a benign pause near VWAP lasts. (Method note: 6 was proposed
from the thesis *before* the sweep — hypothesis-then-test, not curve-fitting.) The advantage concentrates exactly where the thesis predicted — N=3
whipsawed the 06-30 10:33 IWM at −33bp, a trade that *actually* won +46% via trail
(the rule didn't exist yet that day); N=6 lets it run. Typical cost: losing
invalidations exit ~2–5bp worse; worst penalty (07-09 trap, −17bp worse) is an entry
#31 now blocks anyway. **Caveat: the aggregate edge hinges on that one save in a
31-trade sample. Revert triggers: 2 invalidation exits worse than −45%, or any
−70% hard stop that a 3-bar exit would clearly have caught.**

**Watch-pattern (no action yet) — HIGH-conviction bear traps on marginal breakouts.**
2026-07-09 QQQ PUT scored 5/5 (ADX 43 rising, SPY+IWM agree, early, calm tape) but
entered just **$0.02** below the buffered ORB-low and V-reversed in ~1 min → invalidation
exit at −12% → −$52 gross / **−$82 net** (13-lot HIGH size amplified the fee hit). Not a
strategy flaw — a fakeout. *If a cluster of "5/5 → immediate invalidation whipsaw on a
razor-thin breach" appears, that argues for a wider `ORB_BREAKOUT_BUFFER_PCT` and/or
capping size when the breakout margin is thin.* Just logging it; act only on a pattern.

| # | Idea | Evidence so far | What would confirm / kill it |
|---|------|-----------------|------------------------------|
| 1 | **Startup position discovery** | Restart orphaned open positions on **both** 2026-06-29 and 2026-06-30 — the fresh process lost `active_trades`, so it didn't manage or EOD-flatten them. (2026-07-01: restart happened pre-market, no orphans.) | This is an operational bug, not a hypothesis — do it before going live regardless of data. |
| 2 | **Cross-symbol agreement as internal breadth** | 2026-06-30: all 3 symbols CALL (agreement) → trend → +$645. 2026-06-29: IWM PUT vs QQQ/SPY CALL (disagreement) → chop → +$13. **Counter-evidence 2026-07-01:** all-CALL agreement day *reversed midday* → −$305. Agreement reads the regime *at entry time* but can't detect an intraday flip. $TICK/$VOLD still logs *"insufficient — bypassed"* on every trade — dead weight on this feed. | Refine: agreement as an entry-time filter only, paired with an intraday regime-change exit (see #5). More days needed. |
| 3 | **Exit tuning — R:R asymmetry** | Winners bank ~+45–48% (trail arms at +50%, exits at 90% of peak); losers realize −71/−74% (hard stop). **Breakeven win rate ≈ 61%.** Current record since exit fix: 7W/3L = 70% — profitable but thin. 2026-06-30 also showed ~8.7 pts give-back from peak on trend days. | Fixing the loss side (see #5) matters more than squeezing the win side: cutting losers at ~−30% drops breakeven to ~40%. |
| 4 | **Data hygiene** | `Profit_Pct` column shows `0.40%` instead of `40.38%` (missing ×100). Audit timestamps in local **CDT**, strategy runs in **ET**. **New 2026-07-01:** the 12:00 SPY BUY logged *identical* ADX (29.73), VWAP (747.43), and underlying (747.63) to values recorded ~2h earlier — 3 exact matches suggests possible stale bar data at entry evaluation. | Cleanup whenever; investigate the stale-indicator anomaly if it recurs. |
| 5 | **Thesis-invalidation exit** (price recrosses VWAP → exit) | **All three 2026-07-01 losers** had price back *below VWAP* at their hard-stop exits (QQQ 728.02 < 728.48, IWM 301.29 < 301.51, SPY 746.55 < 746.87). The entry reason ("price > VWAP and > ORB high") was long dead while the bot held to −70%. An invalidation exit would have cut them near −20/−30% — ~$350 saved on one day. | Watch: how often does price dip below VWAP *then recover* on winning trades? If rare, implement; if common, require N consecutive bars below or a buffer. |
| 6 | **Entry quality in chop** (ADX slope + time-of-day + breakout buffer) | 2026-07-01: ADX slope entry→exit predicted outcome **5-for-5** (rising → both winners; falling → all three losers). Both winners entered in the first ~40 min; all losers were midday entries (11:24 ET+) at marginal ADX ~25.5 on micro-poke breakouts. Supports the ADX-slope idea in [TODO.md](../TODO.md). | Track ADX slope at entry (last ~10 bars) + entry time for every trade. If the pattern holds across more days, gate midday entries on rising ADX / wider breakout margin. |

---

## 📊 CUMULATIVE META-ANALYSIS (through 2026-07-09) — "do we lose more than we win?"

**Short answer: yes — 45% win rate. But the strategy is sitting *right at breakeven*,
and fees are what push it into the red.** Not broken; underwater at the margin.

### The numbers (current era, fixed exits, 06-30 →)

| Cohort | Trades | W / L | Win rate | Gross | Fees | Net | Breakeven WR |
|--------|--------|-------|----------|-------|------|-----|--------------|
| **All current-era** | 29 | 13 / 16 | **45%** | −$122 | $120 | −$242 | 47% |
| **Debit spreads** | 26 | 12 / 14 | **46%** | **+$16** | $85 | −$69 | 46% |
| **Condors** | 3 | 1 / 2 | 33% | −$137 | $36 | −$173 | **86%** |

### What this actually says

1. **The exit-rule work paid off — R:R is now ~1:1.** Avg win +$85 vs avg loss −$77.
   That's the key win: earlier the structure was inverted (small wins, −70% losses)
   needing a 61% win rate. Now breakeven is **47%**. We're at 46% — *two points short*,
   not a mile.
2. **Debit spreads are break-even on gross (+$16 over 26 trades).** The directional
   edge is real but tiny. **Fees ($85) are the entire difference** between flat and
   −$69. This is the GO_LIVE Gate-2 problem, quantified: *the strategy's edge is
   smaller than its transaction cost.*
3. **Condors are a net drag so far.** 33% win rate, and the structure needs **86%**
   to break even (tiny +$12 avg credit kept vs −$74 avg loss when run over). 3 trades
   is a small sample, but the math is structurally unfavorable at this size — a
   $1-wide condor collecting ~$0.30 risks $0.70 to make $0.30. Reconsider (wider,
   or higher `MIN_CONDOR_CREDIT`, or shelve until the debit side is green).
4. **The strategy only truly wins on clean trend days.** Day-by-day win rate: 06-30
   **100%** (trend) → then 40%, 25%, 50%, 29%, 25%. Six of the last seven sessions
   were chop/reversal, where it treads water at best. The edge is real but *regime-
   dependent and thin*.

### The path across breakeven (both already in the backlog)

- **Win rate 45% → 50%+**: entry quality — the `MIN_CONVICTION_SCORE=2` skip (#17,
  done), ADX-slope gate (done), and skipping negative-conviction days. A few
  percentage points is all that's needed.
- **Fees down**: fewer/bigger high-conviction trades, the +60% target capturing more
  per win (#19, done), wider spreads (#20). Halving the $120 fee line alone flips
  the current era from −$242 to roughly −$120.

**Bottom line:** this is not a broken strategy — it's a marginal one being sunk by
transaction costs. Two small, already-scoped pushes (a few points of win rate + a fee
cut) move it from "loses slowly" to "roughly flat," and only then does trend-day
upside make it net positive. Do NOT go live until the *fee-adjusted* number is
consistently green (GO_LIVE Gate 2).

---

## 📉 WEEK 1 REVIEW (07-06 → 07-10) — "where are we wrong?"

**6W/17L (26%), ≈ −$729 strategy gross, ≈ −$1,450 all-in** (fees ~$180, bug losses
~$690+). Half the damage was software (#21 orphans, #30 double-fill), half strategy.

**The four-layer diagnosis (evidence-ranked):**
1. **Regime mismatch (core).** Zero sustained trend days in 8 sessions since 06-30.
   14 of 23 exits were failed-breakout invalidations — losses are *directionally
   systematic* (breakouts fade), the signature of a mean-reverting tape. Chop days
   cost ~$100–250 even with guards; one trend day pays ~+$650. Viable at 1-in-3
   trend days; bleeding at 1-in-8.
2. **Entries are late by construction.** ADX(14)-rising + ORB + buffer = enter after
   30–60 min of confirmation → maximum crowd → prime fade. The two 5/5 bear traps
   (0W/2L, −$187 + ~$58 fees, both <$0.10 past trigger) are this flaw expressed.
3. **Condors mistimed — decided.** 1W/5L, ≈ −$245: "proven chop" at 11:00 is exactly
   when midday ranges break into afternoon trends. Plus 86% structural breakeven WR.
   → **Disable (#28).**
4. **Conviction sizing pays up for the most fade-able signals.** HIGH = 0W/2L this
   week at 13–15 lots. → Cap `CONVICTION_HIGH_MULT=1.0` until the tier earns it.

**Actions:** CONDOR_ENABLED=false ✅ (applied 07-10) · HIGH mult → *user decision: KEEP
1.5× and fix the entries it amplifies instead* → #31 path-aware entry guard ✅
(implemented 07-10) · fix #30 before next run (still open).
**Trigger watch:** only winners entered 10:19–10:29 ET; if next week repeats, restrict
entries to the morning session. **If failed breakouts persist 2–3 more sessions,
stand down rather than keep paying fees to lose** — a trend system in a no-trend
regime should trade rarely (→ VIX1D filter #8 becomes the priority).
**Not concluded:** strategy death, or flipping to fade-the-breakout. 23 trades is one
regime's sample.

---

## 2026-08-21 (Fri) — 🟢 +$590: the THESIS RAIL's first win (Runway CALL) — the human traded, the machine sat out

**The thesis rail's first fired-and-won trade.** Thesis **CALL @ 8.40 (11:02, spot 7679)** → trailing stop
**+70% = +$590** (peaked +90.48%, MAE −39.88%). Setup_Tag **`Runway`**. The mechanical GEX strategy **did
not trade at all** — every signal it formed was filtered (no-momentum / low-vol). So today the *human* read
carried the day while the *machine* sat out — the exact mirror of 08-20 (machine won, thesis arm expired).

**The day — a gap-up reversal, NEAR the flip (not deep neg-γ).** Yesterday closed 7642 at the lows; today
**gapped up** and the OR formed **7660–7678**, above the 7650 short-gamma shelf. Crucially, Gflip was
**7695.91** and spot ~7679 = only **−0.22% below the flip** — NOT the deep −1%+ negative gamma of the prior
days. Near the flip = little directional hedging pressure = the ~75-min chop (7660–7673) before the break.
Then a 2-bar break above the OR high (7678) at 11:00–11:04 → ran to the day high 7693.

**Why the CALL won — Runway, near the flip:** entry 7679 sat above the whole 7640–7665 shelf (Runway), with
an air pocket to 7693 and the flip (7696) just overhead as the natural ceiling. It didn't need to travel
far — ATM gamma did the rest (+90% on a ~14-pt move).

**Three mechanics validated live:**
1. **MAE column earned its keep immediately** — first real reading **−39.88%**. This winner sat down ~40%
   before running to +90%: the break was whippy (11:00 above → 11:01/02 back below → 11:03/04 real break),
   the bot fired 11:01 *into* the pullback, and — with no fixed stop — survived to pay. The 08-20 "let it
   ride" lesson, now visible in the ledger.
2. **The 7665 buffer was moot** (OR high 7678 formed above it) and the **2-bar confirm was a wash** — the
   first approach to 7678 (11:00) was the real one, no earlier fakeout for the 2nd bar to reject. The more
   interesting lever is live-bar-vs-completed-bar confirmation (see [THESIS_GEX.md](THESIS_GEX.md), n=1).
3. **The rail only fires on confirmation** — it correctly waited out 75 min of chop for a genuine break.

**Infra — CORRECTION (verified 08-21 eve):** an earlier draft of this entry said the bot "died ~15:22."
It did **not**. The log shows continuous GEX collection every 5 min through **15:55:08**, then the normal
16:00 market-closed transition and hourly keep-alive sleep (it's alive, waiting for Monday). At the moment
I first looked, 15:22 merely happened to be the newest log line mid-session and I misread it as a death —
my mistake. So **there was NO infra failure on 08-21**; the day ran cleanly through the close, and the
audit shows the running bot is on the new 28-col/MAE code (the SELL row's −39.88% is bot-written). The
bearish PUT arm was still validly pending (its 15:55 expiry hadn't hit) when I removed it manually. **#16
(always-on host) is still the right next step in principle** — a laptop is the long-term risk — but it did
**not** bite today.

**Forward-test tally:** Thesis **+$590** (1st trade). GEX unchanged at **−$228** (4 trades; didn't trade
today). Combined realized **≈ +$362**. Reconcile 08-21 after settlement to confirm (closed on a clean 11:44
fill, so already realized).

---

## 2026-08-20 (Thu) — 🟢 +$810: the first end-to-end GEX win the SYSTEM captured (Runway PUT, trailing stop)

**A green day, three firsts.** GEX **PUT @ $12.80 (10:17, spot 7675.24)** → **auto-closed by the trailing
stop @ $20.90 (14:12) = +$810 (+63.28%, peaked +79.30%).** Setup_Tag at entry: **`Runway`**. Separately,
the human **bullish CALL thesis correctly never fired and expired** — the day was bearish.

**The day.** Gapped down and trended down all session: open 7684, high **only 7695**, low 7642, close 7642 —
**deep negative gamma the whole day** (spot 44→87 pts *below* Gflip ~7729, −0.57% → −1.13%, deepening into
the close). Dealers sell into weakness in neg-γ → the downmove was amplified. Textbook GEX momentum tape.

**Why the PUT won — `Runway` + the trailing stop:**
- Entry 7675 sat *above* a put-support ladder of **7650/7640/7645** → genuine runway to fall *into*. Broke
  below the OR-low (7679) with 2-bar downward acceleration, deep neg-γ. Spot fell 7675 → 7652, right into
  the support zone.
- Peaked **+79%**; the trail (arms +50%, gives back 20% of peak) exited at **+63% = +$810**. **No take-profit
  cap clipped it** — the "let the convex tail ride" design worked exactly as intended. Contrast 08-19, which
  peaked only +28% (never armed the trail) and bled to −80%.

**Three firsts:**
1. **First GEX winner the *system* actually captured end-to-end.** 08-17 won but was a *manual* close (tick
   bug); the two IntoWall losses were catastrophe-stops. Today the trailing stop did its job unattended.
2. **H3 (Runway vs IntoWall) is now 3-for-3 in the predicted direction:** `Runway` **2-0** (+$877, +$810 =
   **+$1,687**) · `IntoWall` **0-2** (−$800, −$1,115 = **−$1,915**). Today's PUT was the *same instrument*
   that lost twice last week — the **only** difference was the Setup_Tag (entry above the support ladder with
   room to fall, vs below the wall shorting into support). Sharpens the **IntoWall guard (#43)**: had it been
   live, GEX over these four trades would be **+$1,687, not −$228**. Still n=4 — keep collecting, don't code it.
3. **The two-system architecture justified itself by disagreeing.** Today the *human* read was wrong (bullish)
   and the *machine* was right (PUT); on 08-18/08-19 it was the mirror. Neither is reliably better — which is
   exactly why running both (mechanical + trigger-gated human thesis) is right. The OR-breakout trigger made
   the wrong thesis **cost nothing** (it required an up-break that never came → expired harmlessly). That is
   the thesis rail's first live outing, and it worked — by *not* firing.

**Running GEX P&L: +877 −800 −1,115 +810 = −$228** over four trades. Still red, but cleanly split: both
`Runway` trades won, both `IntoWall` trades lost. **Reconciled: settled `dailyPnL` = +$806.74** (gross
$810, fees $4.89, net $805.11; 2 orders, 0 orphans).

**⭐ The no-fixed-stop decision (08-17) earned its keep today.** The winner spent **~3 hours underwater**:
entered 10:17 @ spot 7675, spot then *rose* to **7692.88 by 11:01 (+17.6 pts against us)** and chopped above
the entry until ~13:12 — max profit was +0.78% at 10:20, and didn't make a new high until +8.98% at **13:38**.
Estimated option drawdown at the low ≈ **−50% to −60%** (marks not logged), and it **never hit the −80%
catastrophe stop** before reversing to +79%. **Under the OLD −50% fixed stop this trade would have been cut
around 11:00 → today's +$810 winner becomes a ~−$640 loss.** A ~$1,450 swing, straight from the 08-17 call
to let the convex tail ride. (The flip side, 08-19, shows the cost side of the same rule — but net, letting
it ride is paying: it's what separated the +79% here from a mid-morning stop-out.)

**The bearish case we skipped (from the 08-19 thesis) would have worked — and we didn't miss it.** We armed
only the bullish CALL and skipped the "break <7700" PUT because 7700 had held 2 days. Today 7700 **gapped and
broke hard** (opened 7684, fell to 7642; targets 7685/7650 both hit) — a bearish OR-break PUT would have fired
~10:17 @ 7675, i.e. the **exact trade the mechanical GEX already took (+$810)**. So arming it would only have
*stacked a 2nd PUT* (thesis + gex = 2 contracts, 2× fees, 2× the −55% drawdown), not added edge. **The real
lesson: 7700 was never the rule — Runway-vs-IntoWall is.** Last week a downside entry was IntoWall (support
sat at 7700/7720, right there); today the market gapped *through* 7700 and the support ladder moved down to
7650, so a downside entry at 7675 was `Runway`. Same "PUT below 7700" idea, opposite structure, opposite
result — and the auto-tag caught it (Runway today, IntoWall last week). *Open design Q for the thesis rail:
when a thesis agrees with the mechanical strategy, do we want it to stack a 2nd position, or defer?*

---

## 2026-08-19 (Wed) — 🔴 SECOND straight IntoWall PUT loss (−$1,115): the bot shorted the day's low

**Same mistake as 08-18, bigger loss.** GEX **PUT @ $13.80 (09:50, spot 7706.63)** — a dip below the
OR-low 7711.62 — **auto-closed on the −80% catastrophe stop @ $2.65 (10:54) = −$1,115.** Setup_Tag at
entry: **`IntoWall`**.

**The mistake (user's read, confirmed):** we bought the PUT at 7706, **below the 7720 put-support wall**
(put ladder `7720|7685|7650`). We shorted *into* the support — price had dipped to the day's low (7700.1)
right as we entered, then **bounced off ~7700 and rallied 44 pts to 7744** before closing 7708. Buying a
PUT below the heaviest put-support strike = shorting where dealers defend = the `IntoWall` trap.

**Two days, same wall, same tag, same result:**
| Day | Entry | Put wall | Setup_Tag | Result |
|---|---|---|---|---|
| 08-18 | 7697 | 7720 | `IntoWall` | −$800 |
| 08-19 | 7706 | 7720 | `IntoWall` | −$1,115 |

**Direction was backwards.** The day's real move was UP (7700→7744); the user's **bullish thesis (CALL
above 7700) was correct** — a ~+$947 winner in simulation. The bot took the opposite side, and then
**skipped its own later CALL signals** (positive-gamma / no-momentum) — locked out of the right direction
while holding the wrong one.

**Exit couldn't help — again.** The PUT peaked only **+28%** (at the 7700 low), below the +50% trail-arm,
so the trail never armed and it rode to −80%. Same "never armed → catastrophe" as 08-18. *(Watch, don't
act: is +50% too high to protect these quick pops? Twice now a real in-our-favor move didn't reach it.)*

**Tally:** GEX is now **3 real trades: +$877 · −$800 · −$1,115 = net −$1,038.** The 1 `Runway` won; both
`IntoWall`s lost big. Tiny sample, but consistent and mechanistically sensible. **This is the case for the
mobile channel in one day** — the bot lost $1,115 on an `IntoWall` PUT while the user's `Runway` CALL read
would have won; the intelligence existed, the pipe to act didn't.

---

## 2026-08-18 (Tue) — 🔴 first fully-automated GEX round-trip: a −$800 catastrophe-stop loss (the mirror of 08-17)

**The close-path fix works — and today it closed a loser.** For the first time the bot executed AND
managed a GEX trade end-to-end with no manual help: BUY SPXW PUT @ **10.00** (10:13), then **auto-closed
at 2.00 (13:10) on the −80% catastrophe stop = −$800** (−$803 net). Yesterday the bot couldn't close (the
tick bug); today it did. A milestone on the plumbing; a loss on the trade.

**The setup — and how it mirrored 08-17:**

| | 08-17 (PUT) | 08-18 (PUT) |
|---|---|---|
| Entry spot / Gflip | 7773 / 7777.5 | 7698.7 / 7775.1 |
| **Distance to flip** | **−0.055% (AT the flip)** | **−0.98% (deep neg-γ)** |
| Put/call OI (our ±5% window) | 0.70 | **2.29 — far more put-heavy** |
| Heaviest neg-γ strike (our calc) | **7755** (matches external −$40.08M) | 7720 |
| Net dealer GEX | ≈ 0 (spot on the flip) | strongly negative (spot 62 pts below) |
| Entry-vol | 0.094 | 0.119 (more active tape) |
| Path | chopped to −53%, then rescued | peaked +4%, never armed the trail, bled straight to −80% |
| Close | **manual** +$876.74 (tick bug) | **auto** −$800 (catastrophe stop) |

**Chain structure didn't predict direction.** Today's book was *more* bearishly positioned than
yesterday's (put/call OI 2.29 vs 0.70, net dealer GEX deeply negative vs ≈0 at the flip) — the stronger
"down" setup of the two — and price went **up** (PUT lost). Yesterday's put-heavy chain → down → win;
today's *more* put-heavy chain → up → loss. The GEX chain is a *prior*, not a forecast. (Nice cross-check
though: our own GEX math independently put the heaviest neg-γ strike at **7755** on 08-17, matching the
external −$40.08M reading — from a different, smaller chain subset. Numbers are our ±5%/3-expiry window,
not the provider's full-chain $; 08-17 is a morning-only snapshot; trust the direction, not the decimals.)

Today was the **textbook** GEX setup the distance-to-flip idea likes — spot ~76 pts (−0.98%) below Gflip,
decisively negative gamma, a clean OR breakdown, good vol — and it **lost**. Yesterday's marginal
near-flip setup **won**. So across the two trades we have, distance-to-flip is **anti-predictive** (the
"good" distance lost, the "bad" one won). Reinforces the call to just collect, not gate.

**Why it lost:** even in deep negative gamma the breakdown **didn't follow through** — spot broke 7698
then chopped/reverted around 7700 all day (regime CSV: spot 7696–7710, never fell). Negative gamma is a
*prior* (odds favor follow-through), **not a guarantee**; today it didn't pay, and a naked PUT bleeding
theta on a sideways tape goes to zero. The honest face of GEX-with-no-backtest.

**The "let the tail ride" change (08-17) showed its downside today — exactly as flagged.** With the 50%
stop and the invalidation exit removed, a straight-loser has no protection until −80%. **Both** removed
exits would have saved money today: the invalidation cut would have exited early (the OR break failed
minutes in), and the 50% stop would have capped it at ~−$500 vs −$800. On 08-17 those same exits would
have cut a *winner*; today they'd have saved a *loser* — the asymmetry you can't escape with one rule.
**Two GEX trades now: +$876.74 (manual) and −$800 (auto) = ~+$73 net** — one a manual rescue. No edge yet.

**Wins today:** (1) the always-on GEX collection worked — `data/gex/regime_2026-08-18.csv` has 72
snapshots all day including **34 while in-position** (exactly the in-trade data we lost on 08-17);
(2) the close path auto-closed cleanly; (3) the catastrophe stop capped the loss at −80%, not −100%.

---

## 2026-08-17 (Mon) — 🟢 FIRST clean fill + a manual +$876.74 win — but the bot couldn't close (fixed)

**The first single-leg trade actually executed end-to-end on the entry side, booked a profit — and
exposed that the exit path was still broken.** First green in a while, with a big caveat.

- ✅ **Entry worked:** GEX PUT signal 09:59 (neg-γ, close 7773 < OR 7774, 2-bar accel), filled 1× SPXW
  7775P @ **$8.60**. The entry tick fix delivered our first clean single-leg fill.
- 🐛 **Close path had the SAME tick bug** (the entry fix was incomplete): `close_position` passed the
  price **raw**, so every SELL bounced with **error 110**, then **error 103** (duplicate-id on the
  reprice-modify). 4 attempts → give-up → "close manually" Discord alert. **The bot could not exit all
  day** — not the invalidation (−4%), not the −50% stop, not the 3:55 flatten.
- 🖐️ **You closed manually @ 17.40 = +$876.74** (booked to `audit.csv`, reconciled by permId) — and
  cleared the assigned-stock residue too (+$3,337 QQQ, +$252 SPY). Account's finally clean.

**The honest part — the profit was luck, not the system.** The bot's intended exit was a **loss at every
trigger**: invalidation wanted out at **−4%**, and it drifted to the **−53% hard stop** by 12:18. Only an
afternoon reversal (SPX → ~7758) turned the PUT into a +100% winner you captured by hand. A 0DTE long
that can't be closed can go to zero — we dodged a bullet.

**Fixes shipped:** `broker.snap_to_tick(symbol, price)` now applied to EVERY option limit (entry + close +
reprice); fixing 110 also kills the 103 cascade. The new **data-farm alert fired correctly** on two
blackouts today (11:19, 14:25 — both self-healed). All 8 suites green.

**Strategy change (user call) — GEX now LETS THE TAIL RIDE.** Removed the invalidation cut and the fixed
50% max-loss stop (they'd have guillotined today's winner at −4%). GEX exits are now: **trailing stop**
(arm at +50% peak, exit on giving back **20% of peak** → e.g. peak +100% exits +80%) · **wide −80%
catastrophe backstop** (a straight-down trade can't ride to a full-premium loss) · **3:55 flatten**.
Rationale: don't cut convex winners early. **Risk, stated plainly:** a trade that never peaks has no
protection until −80% (~−$690/contract). GEX has **no backtest** (learning: we never had GEX data) — so
this is a forward-test judgment, watch it. Trend keeps its own 50%-stop/reversal/EOD exits.

---

## 2026-08-13 (Thu) — 🐛 the FIRST real single-leg signal fired — and 3 bugs blocked it (all fixed)

**Chop day, 0 trades booked — but for the first time that wasn't the filters, it was a bug.**
At **10:01** the maiden GEX signal of the whole forward test fired: `GEX CALL: wall-breakout
@7800` (close 7802 > OR 7787, 2-bar accel, entry-vol 0.082). It tried to buy 1× SPXW 7800C at
limit 11.65 and **failed to execute** — three independent defects on the single-leg path:

- **(A) IBKR error 110 — the fatal one.** `option_tick` returned $0.05 for SPX at all prices,
  but SPX options tick **$0.10 at premium ≥ $3**; 11.65 isn't a valid $0.10 increment → rejected.
- **(B)** `notify_submit` crashed (`NoneType.__format__`) — single-leg indicators carry
  `adx/vwap/orb = None`, and `.get('adx', 0)` returns `None` (not 0) when the key is present.
- **(C)** `notify_filled` dereferenced `trade['short_strike']` — a KeyError single-leg would hit
  on fill (latent today; the order never filled).

**(A)+(B) would have blocked every single-leg order (trend AND gex) — so across the entire
forward test, no single-leg trade could ever have executed.** Fixed + unit-tested
(`test_single_leg_execution`): price-aware `option_tick(symbol, price)` + tick-snapped limit,
`(x or 0)` coercion + single-leg-aware notify templates.

**What did the miss cost? Nothing — it dodged a loss.** BS-replay along today's real path with
live GEX exits: entry 11.65 → **peaked 19.21 at 10:33 (+$756 unrealized)** → round-tripped as
SPX mean-reverted to ~7791 → **50% stop ~11:18, net ≈ –$650**. Textbook chop-day round-trip: the
no-TP design gave the spike back. The bug cost **$0** today (saved ~$650); it matters for the next
**trending** day, where that same uncapped tail is the edge — *that's* the day the fix pays.

**Also (08-12 context, not previously logged):** first day the bot actually *evaluated*
post-MIDPOINT-fix — 69 regime reads, 19 correct skip-alerts, and a self-healed **~18-min data-farm
blackout** (10:06–10:24, IBKR 2103/2105). Added a **data-feed drop/restore Discord alert**
(on-change, deduped) so a future blackout pings instead of hiding in the console, + a pandas-2.x
chained-assignment cleanup.

**Where we stand:** the forward test still has **zero clean single-leg executions** — the next real
signal (after restart) is the true first. Both filters keep correctly flagging chop (GEX
positive-gamma all day, trend kauf > 50). Don't read the empty ledger as evidence about the
*strategy* yet — the machinery is only now actually able to trade.

---

## 2026-08-06 — 🔄 FRESH START: the "honest last try" — INITIAL breakout, follow, SPX-only

**Decision (user):** one more month, clean slate, with the user analyzing trades hands-on this
time (not relying on my retros). Rationale that earns the shot: **we tested INITIAL on
SPY/QQQ/IWM but never on SPX** — and SPX is ~4× cheaper on fees, which is the whole game. The
old ledger is archived (`audit_archive_2026-06-30_to_2026-08-06.csv`, 103 trades); `audit.csv`
reset to header only; account balance reset (user, in IBKR).

### The config baseline (what "INITIAL" means here)
Pure original breakout, **following** (not flipped), every filter we added after 06-30 turned OFF:
| setting | value | = |
|---|---|---|
| `SYMBOLS` | SPX | cash-settled, cheapest fees |
| `FLIP_DIRECTION` | **false** | follow the breakout |
| `ADX_SLOPE_BARS` | 0 | no rising-ADX gate |
| `ORB_BREAKOUT_BUFFER_PCT` | 0 | no breakout buffer |
| `PATH_FRESH_BARS` / `PATH_MOMENTUM_BARS` | 0 / 0 | no #31 path guard |
| `VWAP_INVALIDATION_BARS` | 0 | no invalidation exit |
| `CONVICTION_SIZING_ENABLED` | false | no min-score gate, fixed size |
| **kept** | | ADX>25 base signal · hard-stop −70% · trail@+50% · TP +60% · EOD flatten 15:55 · daily-loss $800 · entry timeout 120s · condors off |

### The honest test — success criteria, set NOW so we don't move goalposts
- **What we're testing:** does the *original* follow-the-breakout premise clear fees on SPX,
  where it's never run? (Backtest said INITIAL-follow was −80 bp net — this is the live check
  of that, with the user reading every trade.)
- **Duration:** ~4 weeks / until ~30–40 trades, whichever first.
- **Call it a WIN if:** fee-adjusted net is ≥ ~breakeven and trending up, OR the user's trade-
  level analysis surfaces a concrete, fixable pattern (that's the real prize).
- **Call it DONE if:** net < −$500 paper, or a clear ~consistent "following loses" pattern
  (which would *reconfirm* the flip finding — at which point `FLIP_DIRECTION=true` is one toggle).
- **Confound to control:** bot uptime (#16). A fair test needs it *running* — down-days poison
  the sample. Keep it alive (tmux + caffeinate at minimum).

### Watch-list for the user's own analysis
Per trade, look at: entry vs the day's eventual direction (were we on the right side?), how much
of the move we captured before the exit, and **fees vs gross** (do wins clear the ~$6–10 fee?).
The audit logs all of it (ADX, VWAP, ORB, conviction-for-reference, commission, peak%). One clean
month of *your* eyes on the tape beats another month of my proxies.

---

## 2026-08-06 — 📉 The flip drought: 0W/2L live + days of no trades (three separate causes)

**Since going flipped (07-28), the live record is 0W/2L, −$66.76, plus a week that mostly
didn't trade.** The frustration ("no trades these days") is real — but the cause isn't one
thing, and that matters for the fix.

### The record 07-28 → 08-06
| day | what happened |
|---|---|
| 07-28 | SPX PUT (flip) **−$46.52** — CALL signal was right, fade lost |
| 07-29 | 0 trades — 2 orders **timed out unfilled** (fill problem) |
| 07-30, 07-31, 08-03 | **bot DOWN** (no logs — host reliability #16) |
| 08-04 | SPX PUT (flip) **−$20.24** — held 81s, **commission $10.24 > the $10 gross loss** |
| 08-05, 08-06 | 0 trades — **filters blocked everything** (path + chop guards, 0 submitted) |

### The "no trades" has THREE causes — removing filters fixes only one
1. **Bot down** (07-30/31, 08-03) — the always-on-host gap (#16). No config change touches this.
2. **Fills timing out** (07-29) — orders submitted, didn't fill in 120s, #34 cancelled them.
   Removing filters makes this *worse* (more unfilled orders), not better.
3. **Filters blocking** (08-05/06) — quiet, rangey days where the guards (and the base ADX>25 +
   ORB breakout requirement) found no clean setup. *This* is the only piece removing filters
   would change — it'd generate more entries on these days.

### The live flip: 0W/2L, and fees are still the story
Both flip trades lost, and on 08-04 the **commission ($10.24) exceeded the gross loss ($10)** —
fees remain the dominant cost even on a near-scratch trade. n=2 is meaningless for the edge, but
it's a reminder the flip is *breakeven-at-best* after the $6.52–10 fee, exactly as the 07-28
verdict said.

### The honest state
We've now run this strategy on every axis — filters on/off, flip on/off, XSP→SPX — and **no
configuration convincingly clears fees** (see the net comparison logged with this entry / in the
response). The drought is part host-downtime (#16), part fills, part genuinely-quiet days. The
"rip out the filters" instinct scratches only the third, and by our own backtest the un-filtered
*follow* version is the worst config of all. See the decision below.

---

## 2026-07-28/29 — ✅ FIRST LIVE FLIP TRADE on SPX — #41 confirmed + the real commission is in

**The month-long questions got answered.** The first flipped SPX trade executed 07-28; today
(07-29) was quiet. Three landmarks, one honest verdict.

### 07-28 — the first BASELINE-flipped SPX trade
Signal fired **CALL** (Bullish, SPY 741.07 > VWAP), flipped to a **PUT** spread. Entry $2.30 →
invalidated at 6 bars → exit $1.90. **−$40 gross, $6.52 commission → −$46.52 net.**

1. **✅ #41 CONFIRMED — SPX trades end-to-end.** The combo **filled clean, no error 478**
   (entry $2.30, exit $1.90). The `bag.symbol = symbol` fix works live. SPX trading is unblocked.
2. **✅ The flip works as designed.** Audit reason: `FLIP #42: signal CALL → exec PUT`. A
   bullish signal correctly opened a PUT.
3. **✅ The real SPX commission is in: $6.52 round-trip** (1-contract 5-wide spread; entry
   $3.26 + exit $3.26). That's **~2.8% of the $230 debit — ~4× cheaper than XSP** (~12%). The
   SPX thesis (#40) is confirmed.

### 💰 The #42 verdict — fee eats ~85% of the edge → **breakeven, on a knife's edge**
Converting to the backtest's units: $6.52 ≈ **~1.9 bp** on this trade's size. The flip's gross
edge was **~+2.2 bp/trade** (+50 bp / 23 trades). So **net ≈ +0.3 bp/trade — essentially
breakeven, before slippage.** Exactly the knife-edge we predicted: SPX fees are low enough that
the flip isn't *dead*, but they consume almost the whole gross edge. **Not a clear winner.** It
needs a real sample to see whether the +2.2 bp gross even holds live (the proxy may be
optimistic), and slippage could tip it negative.

### This trade was a *loss the flip caused* — the regime risk, live (n=1)
The original CALL signal was **right** — SPY rose (that's *why* the PUT invalidated). Following
would have won; fading lost. One concrete instance of "the flip is wrong when the breakout
follows through." Meaningless alone (n=1), but a reminder the variance is real — don't read one
trade either way.

### 07-29 (today) — 0 trades
Bot up all day. **2 orders submitted, both timed out unfilled** (#34 cancelled them — no adverse
fills), everything else blocked by the filters (53 chop-guard + 36 path-guard). The timeout did
its job (no dead-signal fills), but the net is **no participation.** ⚠️ **Watch:** 2 SPX
non-fills in a day despite the tight $0.10/leg spreads — if SPX limits keep failing to fill,
we'll starve for trades. First sign of it; monitor whether it's the flipped-limit pricing or
just a slow tape.

### Net
The plumbing is **done and proven** (#41, SPX, flip all live and correct), and we finally have
the number that gates everything: **$6.52/trade → the flip nets ~breakeven after fees.** So the
verdict on #42 is not "win" or "dead" — it's **"thin, unproven, keep gathering real fills."**
Two things to watch: (a) does the gross edge hold over a real sample, and (b) the SPX fill rate.

---

## 2026-07-27 (Mon) — 🟢 SPX went live · 🐛 but the first SPX order was rejected (contract-symbol bug)

**First day live on SPX** (restarted onto it after the 07-22 validation). Good news up front:
**sizing worked exactly as designed** — a MEDIUM 3/5 signal → **1 SPX spread @ $2.40 debit**
(the validation predicted ~$2.15; MEDIUM budget $400 → 1 spread ✓). But **the order was
rejected**, so **0 fills**. No completed trade since the 07-20 win (07-21 blocked, 07-22 no
trades, 07-23–24 bot down during the migration, weekend).

### 🐛 P0 — SPX combo orders rejected (IBKR error 478). *SPX cannot trade until fixed.*
```
error [478]: Parameters in request conflict … Requested symbol SPXW, in legs SPX
```
`make_bag_multi` sets the BAG's `symbol` to the **option root** via `option_symbol()` →
`'SPXW'`. But the qualified legs are options on the **underlying `SPX`**. IBKR requires the
combo's symbol to be the underlying, so it rejects the SPXW/SPX mismatch.
- **Only SPX is affected.** For XSP and SPY the option root *equals* the underlying
  (`XSP`/`SPY`), so `bag.symbol` matched the legs and combos worked (proven live). SPX is the
  first product where **trading-class (`SPXW`) ≠ underlying (`SPX`)**, which is exactly what
  478 is complaining about.
- **Fix (one line, additive-safe):** in `make_bag_multi`, `bag.symbol = symbol` (the
  underlying) instead of `self.option_symbol(symbol)`. No change for XSP/SPY (root == symbol);
  fixes SPX. The comboLegs (by conId) and the option_exchange routing were fine.
- **Why validation missed it:** `validate_xsp.py` qualifies *single legs* and prices via
  `get_spread_value` (also single legs) — it never qualifies/places a **BAG**. The bug lives
  only in the combo. **Lesson: the validator must also qualify a BAG combo** (add a
  `reqContractDetails(make_bag(...))` check) so this class of bug can't reach a live order again.

### Everything else: same #39 wall
With SPX finally live, the day was still **56 chop-guard blocks + 54 path-guard blocks** —
the anti-trend entry filters (#39) strangling entries exactly as on 07-21. Even the one
signal that got through was a MEDIUM 3/5 on `slope✗` (ADX high but falling, 40→35) — the
override (`ADX_SLOPE_OVERRIDE_ADX`, still 0) would have mattered, but the 478 bug made it moot.

### Net
A half-step forward: **SPX is live and sizes correctly**, and the one blocker is a
one-line contract-symbol fix (plus a validator gap to close so it doesn't recur). But we're
now **7 sessions without a completed trade** (blocked, down, or rejected), and the entry
filters (#39) remain the thing keeping us out of the market. **Immediate: fix the BAG symbol
(#41) + harden the validator; then #39 so SPX actually gets to trade a trend day.**

---

## 2026-07-21 (Tue) — 0 TRADES on a clean +0.36% BULL TREND 🔴 — the guards strangled our best day

**The one regime this strategy is built to win — a clean trend day — and we didn't place a
single filled trade.** SPY ground **up +0.36%** (high +0.46%, low −0.19%: shallow dips, steady
grind). Zero participation, $0. This is the most important "quiet" day in the journal.

### Why we sat out (block tally, all-day)
| count | blocker |
|---|---|
| **22** | **#31 path-freshness guard** — `level 747.44 not crossed in last 10 bars — stale break / hovering` |
| **13** | ADX-slope chop guard — `ADX … flat/falling` |
| 4 | signal cooldown |
| 1 | the ONE order (11:23, MEDIUM 2/5 CALL @ $0.48) — **timed out unfilled (#34)** |
| 2 | conviction below minimum |

### The core finding — the #31 freshness guard is *anti-trend by construction*
The path guard (built 07-10 to stop chop-day bear-traps) requires the breakout level to have
been **crossed within the last 10 bars** — i.e. a recent close on the *other* side of the level.
**A healthy sustained trend never provides that:** price broke above XSP's 747.44 ORB early and
*stayed* above it, so from ~11:30 on, every CALL was blocked as a "stale break / hovering" —
for the rest of the day. The mechanism can't tell a *stale hover in chop* (flat at the level)
from a *breakout that held* (trending away from it). It treats the second like the first and
vetoes it.

**This inverts our participation vs. regime — the worst possible selection:**
- **Chop day:** price oscillates *around* the level → it *was* crossed recently → freshness
  **passes** → we enter → the breakout reverts → we lose. (This is the flip-test result:
  25% direction accuracy, we buy tops.)
- **Trend day:** price breaks and *holds* → no recent cross → freshness **blocks** → we miss
  the one clean move. (Today.)

So the guard actively selects **for** the regime we lose in and **against** the regime we'd win
in. That's not a tuning nit — it's backwards. Third independent confirmation of the #10/#32
thesis ("the risk/filter layer is over-fit to chop and strangles trend days"), now with the
specific culprit named.

### Compounding factors
- **The ADX-slope gate (13 blocks)** hit the same way: a smooth trend has *high but flat* ADX,
  which the rising-ADX requirement rejects. **`ADX_SLOPE_OVERRIDE_ADX` (built + tested in #32,
  left at 0/off) is exactly this fix** — turning it on would have unblocked some of these.
- **The one order that got through didn't fill:** a passive mid-priced CALL limit on a *rising*
  tape gets left behind, and #34 (rightly) cancelled it at 129s. On a strong trend we may need
  a more aggressive limit (nearer the ask) — but that's secondary to not being blocked at all.

### Fix direction (new TODO #39)
The freshness check should be **waived when momentum strongly agrees and price is decisively
beyond the level** (a held breakout), and only applied when price is *hovering near* the level
(within ~X%) — where the bear-trap actually lives. The momentum sub-check (`PATH_MOMENTUM_BARS`)
already exists to lean on. Pair with turning on `ADX_SLOPE_OVERRIDE_ADX`. Ties to #32/#33.

### Net
0 trades, $0 — but the most diagnostic day since the flip test. We now have the whole loop:
we **over-participate in chop** (lose) and **under-participate in trends** (miss), because the
#31 freshness guard + ADX-slope gate are anti-trend by construction. The direction engine isn't
even the bottleneck here — the *entry filters* are. **#39 + turning on the #32 override are the
next concrete moves.**

---

## 2026-07-20 (Mon) — 1W/0L 🟢 **STREAK BROKEN** (first win since 07-09) + #34 partial-fill fired live

**First green close in 9 trades.** XSP PUT, **+17.14% / +$24 gross → +$3 net.** Tiny, but
it's a win, it was clean, and it live-validated the riskiest code we shipped.

### The trade
| | |
|---|---|
| 13:03:56 | XSP PUT signal, MEDIUM 2/5 (`ADX✓ slope✓ agree✗ early✗ tape✗(28x)`), 8 lots @ $0.35 (≥ the new $0.30 floor) |
| 13:06:05 | **#34 timeout fired at 129s** — order unfilled, cancelling… |
| 13:06:06 | **…but 4/8 lots filled as the cancel landed → partial-fill RESCUE tracked the live 4-lot slice** (not orphaned) |
| 13:07–13:15 | ran to **+48.57% peak** |
| 13:23 | invalidated (6-bar VWAP recross) → closed +17.14% / **+$24 gross**, $13.92 fee |

### Findings
1. **⭐ The #34 partial-fill rescue path worked in production.** The exact edge case I unit-
   tested (`test_entry_timeout.py`): the timeout cancel raced a fill, 4 of 8 lots filled, and
   `_expire_stale_entry` requantified to the real 4 lots, promoted to ACTIVE, parked the TP,
   and managed it to a clean profitable close — **no orphan, no over-close** (the #21/#30
   failure class). First live proof that path is correct. That it produced our first win is
   a nice coincidence, not the point.
2. **💸 Fees ate 87% of the gross — on a WINNER.** $20.88 commission ($6.96+$13.92) vs $24
   gross → **+$3 net**, i.e. **~$0.78/lot** on a +17% winning spread. XSP fees run ~$5/lot
   round-trip, so a +17% win barely clears them. **Even when we're right, fees eat almost
   all of it.** This is the fee wall (#20/#35) in its sharpest form yet: we need *bigger* %
   wins (wider spreads → fewer contracts) or the math never works. (Note: the partial fill
   halved the trade — full 8 lots would've been ~+$6 net, still a rounding error.)
3. **✂️ The invalidation capped a real winner.** Peaked **+48.57%**, but the trailing stop
   only arms at **+50%** — missed by **1.4 points** — so it got no trail protection and the
   VWAP recross booked it at +17% on the giveback. **Consider lowering
   `TAKE_PROFIT_TRAIL_TRIGGER` to ~0.40–0.45**: our winners peak and revert fast (the replay/
   flip work says the same), and a +48% peak deserves better than a +17% exit. New TODO.
4. **🖥️ Bot was DOWN all morning (#16).** Startup failed at 10:05 (`ConnectionRefused` on
   4002 — Gateway wasn't up) and only came online ~13:00, so we missed the entire morning
   session and took just this one afternoon trade. The always-on-host gap keeps costing
   sessions; still the top operational debt.
5. **Same marginal 2/5 entry — but this time the tape followed through.** `tape✗(28x)`,
   `agree✗`, `early✗` — indistinguishable from the losers; for once the PUT's down-move
   didn't immediately revert. One right call inside a stretch of wrong ones; doesn't move
   the flip-test conclusion (we're still directionally poor), but the streak is snapped.

### Net
A genuine win (+$3), a **live validation of the partial-fill rescue**, and a razor-sharp
fee lesson: **+17% gross netted $0.78/lot.** The mechanics are working now; the economics
(fees) and the entry direction are still the two walls. **Next levers:** lower the trail
trigger (new #38), and the fee ratio (#20 wider spreads).

---

## 2026-07-17 (Fri) — NO TRADE ✅ #34 fired · Friday check-in · 🔄 the flip test

**No loss today — for a good reason.** The one signal (10:05, XSP CALL, **HIGH 4/5** — the
best conviction in two weeks: `ADX✓ slope✓ early✓ tape✓(1x)`) submitted at $0.48, **didn't
fill, and #34 auto-cancelled it at 129s** ("signal is stale; cancelling"), then cooldown
kept us out. Counterfactual: after 10:05 SPY went +0.26% then **−0.43% (close −0.29%)** —
the CALL would have **lost**. **The timeout fix saved a losing trade on its first live day.**
(And note: even our best-conviction CALL was on the wrong side again — a PUT would have won.)

### Friday check-in — the week (07-13 → 07-17)
| Day | Result |
|---|---|
| 07-13 | 0W/2L (IWM+SPY PUT) −$155 · + assignment cleanup |
| 07-14 | 0W/1L (XSP CALL) −$42 · **first live XSP trade — migration works** |
| 07-15 | 0W/1L (XSP PUT) −$80 net −$175 · stale-fill + fee-bomb day |
| 07-16 | 0W/1L (XSP CALL) −$77 |
| 07-17 | **0 trades** — #34 cancelled a would-be loser |
**Week: 0W/5L. Losing streak since the last win (07-09): 9 trades. Era net: −$1,752.**
But the *machinery* got materially better: condors off, XSP-only (no assignment), #30/#31
fixed, **#34 timeout fired and worked**, **#35 fee floor raised** (`MIN_SPREAD_COST` 0.10→0.30).
We're plugging the leaks; the edge is still missing.

### 🔄 The flip test — "buy PUT when logic says CALL, and vice versa"
Ran it honestly (`scripts/flip_analysis.py`): every real debit entry, re-simulated under the
**real exit rule**, as-taken vs direction-flipped (not a naive sign-negation — a call spread
flipped to a put spread has different fills *and* a different invalidation hold time; this
re-simulates both). Underlying-move proxy, **fees excluded**:

| window | NORMAL (as-taken) | FLIPPED (CALL↔PUT) |
|---|---|---|
| Full era (06-30+, 36) | −82 bp · 31% | +104 bp · 47% |
| **Last 2 wk (07-03+, 24)** | −77 bp · **25%** | **+110 bp · 54%** |
| Last week (07-13+, 5) | −44 bp · **0% (0W/5L)** | +30 bp · **80% (4W/1L)** |

*The flip edge intensifies the more recent the window — direction accuracy fell to **25%**
over the last two weeks (right 1 in 4). Still not "profit": 54% < the ~58% gross breakeven
(worse after XSP's ~7%-of-position fees), and it's a regime bet — a trending week flips it
back. Run `scripts/flip_analysis.py [YYYY-MM-DD]` to re-window.*

**What it means (and doesn't):**
- ✅ **Real finding: our direction engine is net anti-predictive on this sample.** 31% → 47%
  win and −82 → +104 bp just by flipping. We are a **breakout-momentum** system running in a
  **mean-reverting tape** — we buy the breakout, it reverts, we're systematically on the
  wrong side. Today's HIGH-4/5 CALL that a PUT would've won is the same story.
- ❌ **NOT "flip it and we're rich."** 47% win at a +40/−55 payoff is still **below the ~58%
  gross breakeven** for that payoff, so +104 bp is barely above water — and **fees (~$380
  era, identical for both books) would eat it**. The flip roughly *neutralizes the directional
  bleed* (turns a clearly-losing book into a coin-flip); it does not clear the fee/whipsaw
  wall. That wall is the moat, and it's why "just invert a loser" is a classic trap.
- ⚠️ **Regime-specific.** The flip wins *because* the recent tape mean-reverts; in a trending
  tape the same flip (= fade the breakout) would lose. So the lesson isn't "reverse the sign,"
  it's **"we don't know whether to follow or fade — we need regime detection"** (loops back to
  #33 entry quality / anchored VWAP). Logged as a hypothesis, not a change.

### Housekeeping
🐛 Fixed: `scripts/test_entry_timeout.py` was calling `_activate_entry` for real → it wrote 2
junk rows to `audit.csv` and fired Discord. Now stubs `audit.record`/notifier; the 2 rows were
removed from the ledger (98 rows, clean).

---

## 2026-07-15 — 0W/1L 🔴 — a 103-minute stale fill + fees > the loss. **8 straight losers.**

**This looks like "one small loss." It isn't — it's the most diagnostic day we've had.**
One XSP PUT: **−$80 gross, −$95.43 commissions → −$175 net.** The fees were **bigger than
the loss**.

### 🚨 The streak (user called it, ledger confirms)
Last winning trade: **2026-07-09**, IWM condor, **+$12**. Last win of any size: **07-07,
+$51**. Since then: **8 consecutive losses, −$544 gross** (07-10 ×4, 07-13 ×2, 07-14 ×1,
07-15 ×1). We are not "thin-edge coin-flip" anymore — we're 0-for-8.

### What actually happened today (the timeline is the story)
| time | event |
|---|---|
| 12:04:36 | Signal: XSP PUT, **MEDIUM 2/5** (`ADX✓ slope✓ agree✗ early✗ tape✗(20x)` — a 20-cross chop tape) |
| 12:04:42 | Limit BUY submitted @ **$0.14** (the quoted mid), 21 lots |
| 12:04→13:47 | **Order sits UNFILLED for 1h 42m** — 98 "still pending" polls |
| **13:47:15** | **FILLED at $0.14** — 103 minutes after the signal |
| 13:48:20 | **Invalidated 65 seconds later** — "wrong side of VWAP for 6 bars" |
| 13:49:21 | Closed @ $0.10. −28.57% / −$80 gross, **$95.43 fees → −$175 net** |

### Findings
1. **🐛 NEW P0 — no order timeout ⇒ adverse-selection fills.** A working entry limit has
   **no shelf life**. This one rested 103 minutes and only filled *when the spread decayed
   to our bid* — i.e. we get filled precisely when the market has moved against the thesis.
   That's textbook adverse selection: a resting limit fills when it's about to be wrong.
   The 6-bar invalidation firing **65 seconds after fill** isn't a whipsaw — the thesis had
   been dead for an hour before we were even in. **Fix: cancel an unfilled entry order after
   ~2–3 minutes; the signal has a shelf life measured in bars, not hours.**
2. **💸 Cheap spreads are a fee bomb — this is #20 in its sharpest form.** A **$0.14** spread
   at a $300 budget ⇒ **21 contracts ⇒ 42 legs ⇒ $95.43 in fees**. Do the math on the *win*
   case: at the +60% TP ($0.224) the gross gain is ~$176 — **minus ~$95 fees ≈ $81 net**.
   We were risking a guaranteed ~$95 fee bill to maybe make ~$81. **`MIN_SPREAD_COST=0.10`
   is far too low**: a cheap spread doesn't reduce risk, it maximizes contract count and
   therefore fees. Raise the floor (~$0.30–0.40) and/or cap contracts.
3. **⚠️ The circuit breaker structurally cannot fire.** `MAX_CONSECUTIVE_LOSSES=5`, but
   `consecutive_losses` **resets every day** (`check_and_reset_daily_trade_count`). At
   1–2 trades/day it can never reach 5 — so **8 straight losses across 6 days tripped
   nothing**. The breaker only sees intraday streaks; it's blind to exactly the slow bleed
   we're in. Needs to span days (or a rolling last-N-trades guard).
4. **📉 Correction to my 07-14 call.** I wrote "fill quality looks fine" off a single 65-second
   fill. **That was premature (n=1).** Today is the counter-example: XSP's thin/wide quotes
   mean a mid-based limit often *doesn't* fill, and when it does, it's adverse. The XSP
   fill-quality risk is real — it just took 2 days to show up.
5. **🔴 The entry was marginal again.** 2/5, `tape✗(20x)`, `early✗`, `agree✗` — a chop tape,
   scraped in at the minimum score. Every recent entry is a 2/5 that scrapes the floor.
   With `agree` now structurally unreachable (XSP-only), **`MIN_CONVICTION_SCORE=2` is
   effectively "take almost anything with ADX rising."**

### Audit-integrity note
The BUY row timestamps the **fill** (13:47:15) but carries the **12:04 signal's** indicators
(px 752.03, VWAP 752.35, ADX 31.82) — 103 minutes stale. At the real fill the underlying was
~753.9. Any retro reading that row will misjudge the entry. Same root cause as #1.

### Net
Not a quiet day — a day that named the two things actually killing us:
**(a) we get filled on dead signals** (no order timeout), and **(b) our cheapest spreads
buy the most fees** (fees > the entire loss today; fees > the profit even on a winner).
Both are mechanical and fixable, and neither is the "edge" problem — they're **taxes on top
of** the edge problem (#33). **0-for-8 says stop tuning and fix the mechanics.**

---

## 2026-07-14 — 0W/1L 🟡 — ✅ FIRST LIVE XSP TRADE (migration works), small loss

**The result is a footnote; the milestone is the headline: the bot traded XSP end-to-end
for the first time and it executed cleanly.** One trade, a small loss, uneventful tape.

### The trade
| Field | Value |
|---|---|
| 11:07 XSP **CALL** 6-lot | entry $0.49 → exit $0.42 · **−14.29% / −$42 gross**, −$31 fees → **−$73 net** |
| Conviction | **MEDIUM 2/5** — `ADX✓ slope✓ agree✗ early✗ tape✗(17x)` (scraped in at the min score) |
| Path | peaked **+16.3%** one minute after entry, reversed, **6-bar VWAP invalidation** at −15% |

### Findings
1. **✅ XSP migration validated LIVE (#3 done).** The order routed as `BAG symbol=XSP
   exchange=CBOE`, filled at the **$0.49 limit**, rested a take-profit at $0.78, and the
   close filled at the **$0.42 limit**. **No assignment risk (the whole point), no orphan,
   no untracked position, no errors** (the lone `[202] Order Canceled` is the normal
   cancel-and-resubmit close flow). Dashboard rebuilt (49 trades / 12 days). The `broker`
   index-option path, the SPY-signal/XSP-execution split, and the exit path all worked
   against real IBKR.
2. **✅ Fill quality looks fine on this sample.** The feared XSP wide-spread slippage did
   **not** bite — both legs filled *at* our limit ($0.49 in, $0.42 out). One trade isn't a
   sample, but the worry that XSP is untradeable at 0DTE is not supported so far. Keep
   watching realized-vs-limit over the next few fills.
3. **🔴 Same whipsaw, now on XSP.** Bought a breakout, +16% within a minute, then it
   reversed and the VWAP recross ejected us at −14%. This is the exact 32/33-invalidation
   pattern the faithful replay flagged — the rolling-14 VWAP hugs price, so the recross
   fires on noise. Not an XSP problem; the same entry-quality / VWAP-reference problem
   (**#33**). The trade never should have carried much conviction: `tape✗(17x)` = a choppy
   17-cross tape, and it was a marginal 2/5.
4. **📉 `agree✗` is now structural (expected).** XSP-only removes cross-symbol agreement,
   so the conviction ceiling is **4/5**, and this entry scraped in at the 2/5 minimum on
   `ADX✓ slope✓` alone. Worth deciding whether XSP-only should *raise* `MIN_CONVICTION_SCORE`
   (a 2/5 with `agree`/`early`/`tape` all ✗ is a thin entry) — logging, not acting yet.

### Net
Green milestone, yellow P&L. **#3 is effectively proven live** — assignment risk is gone
and XSP trades clean. The −$73 is the same marginal-entry / VWAP-whipsaw story we already
have a plan for (#32 buffer + #33 anchored VWAP), now reproduced on the new instrument.
*(Fixed same day: `replay_invalidation.py` now resolves the signal source in `day_bars`,
so XSP entries replay on SPY bars — all 34 entries count. New totals: N=6 −70bp
(invalidates 33/34) · +buf0.05% −64bp · +buf0.10% −266bp · hold ~no effect. Same story.)*

---

## 2026-07-13 — 0W/2L 🔴 + 🚨 ASSIGNMENT from Friday — the guards strangled a trend day

**Two separate stories today, and the small one is the spreads.**

### 🚨 Story 1 (the big one): assigned into stock over the weekend
The IBKR account is holding, as of today, **400 QQQ shares** (avg 723.87) and **600 SPY
shares** (avg 754.60) — **~$733k notional, ≈ −$9.0k unrealized**, taking **−$9,810 of
*today's* account dailyPnL** (QQQ −$5,800, SPY −$4,010). These are **Friday 07-10
assignments**: short option legs that expired ITM and were exercised into stock over the
weekend. **600 SPY = the "net-long 6-lot residual" the #30 double-fill left on 07-10**
that wasn't flattened before Friday's close; the QQQ 400 similarly traces to unclosed
condor legs. My initial 07-13 retro said "clean execution, no orphans" — **that was wrong.**
The bot's ledger is blind to assigned stock, so I only saw the −$207 of spread P&L and
missed the ~$9k sitting in the account. **This is the real damage of the week, and it's
an operational/structural failure, not a strategy one.**
- **Root cause:** 0DTE options on **SPY/QQQ/IWM are American-style ETF options → assignable.**
  Any short leg left open into expiry (via a failed/residual close, or a condor held to
  the bell) can become stock. #30 is fixed, but the tail risk is inherent to assignable
  products.
- **Fix (user directive 07-13):** stop trading SPY and QQQ; move to **cash-settled,
  European-style index options (XSP, etc.) that cannot be assigned.** Promotes TODO #3
  from "at live transition" to now. (IWM is also assignable — needs a cash-settled
  replacement too, e.g. MRUT/RUT.) **ACTION: user to flatten the 400 QQQ / 600 SPY
  shares** — the bot can't and won't trade them; they're unmanaged directional risk.

### Story 2: the spreads — 0W/2L, −$207 net, but the loss is a *false negative*
Config: condors OFF, `VWAP_INVALIDATION_BARS=6`, path guard #31 live, #30 fixed.

| Trade | Conviction | Entry→Exit (underlying) | Result | What actually happened |
|-------|-----------|-------------------------|--------|------------------------|
| IWM PUT 10:19 (7-lot) | MEDIUM 3/5 | 293.69 → 294.42 (6 min) | −$91 / −$113 net | shorted a breakdown, bounced to VWAP → invalidated −33% |
| SPY PUT 12:02 (8-lot) | MEDIUM 2/5 (inv−1) | 751.06 → 751.45 (11 min) | −$64 / −$94 net | invalidated at 751.45 — **then SPY fell to ~748.2** |

**The damning part: today was a DOWN day and we had the direction RIGHT on all three
names — and the strategy's own guards turned that into a loss.**

1. **🔴 The VWAP-invalidation exit whipsawed us out of correct trend trades.** We bought
   PUTs (right), price pulled *back* through VWAP on a normal retracement, the 6-bar
   invalidation ejected us at a small loss — **and then the downtrend resumed without us.**
   SPY is the proof: exited at 751.45, SPY closed ~748.2 (−$6.7/sh on the day). The
   751/750 bear put would have run from our 0.28 exit toward its $1.00 max and hit the
   +60% take-profit (~0.576) — a ~**+$173** trade instead of −$94. The exit rule assumes
   "VWAP recross = thesis dead," which is **false on a trend day with pullbacks** — exactly
   the day the strategy is built to win.
2. **🔴 The entry chop-guard blocked QQQ all day — on QQQ's biggest down day.** QQQ fell
   **~$14.5/share (~−2%)** today, but the ADX-slope guard read "ADX high but flat/falling"
   and blocked *every* QQQ entry from 10:07 onward. So the one name that trended cleanly,
   we never traded. The guard meant to avoid chop-day fakeouts also vetoes real trend days
   where ADX is elevated-but-not-rising.
3. **⚖️ Verdict on "entry wrong / exit wrong / rewrite?" → it's the FILTER/EXIT layer,
   not the core signal.** The direction engine was right on SPY, IWM, and QQQ. What lost
   money was the risk scaffolding *we* bolted on over the last two weeks to survive chop
   days (rising-ADX gate, VWAP invalidation, path guard, N tuning): it's **over-fit to
   the chop regime and now strangles the trend regime.** Not a rewrite — a **recalibration
   of entry gate + exit to be regime-aware** (give trend trades room; only invalidate when
   the trend is actually dying, e.g. ADX falling, or use a structural stop at the ORB
   level rather than a bare VWAP tick).
4. **N=6 note:** both invalidations fired at exactly 6 bars, but that's a symptom of #1
   (the exit fired at all), not the disease. Re-tuning N within the same VWAP-recross rule
   won't fix a rule that shouldn't fire on trend-day pullbacks. Revert triggers still not
   hit (−33%/−22%, no −70% stop).
5. **💸 Fees:** $51.72 on −$155 gross (~33%) — still the standing structural drag (#20).

### Net
The spreads *look* like a −$207 no-edge day, but the truth is worse and more useful:
**the direction was right everywhere, and the strategy's own guards + exit converted a
winning-direction down-day into a loss** (whipsawed out of SPY/IWM, gated out of QQQ),
**while Friday's assignment quietly cost ~$9k.** Two action items fall out: (a) migrate
off assignable products to cash-settled index options; (b) make the entry gate and the
invalidation exit **regime-aware** so trend days aren't strangled. See TODO.

### 07-13 evening — #3 built + Gateway-validated; #32 replay tempers the exit thesis
**#3 (XSP) validated** (`scripts/validate_xsp.py`, read-only): XSP qualifies as `IND` on
CBOE (level 751.53 — ~0.5% above SPY, confirming strikes must come from XSP's own level);
option chain present with `tradingClass=XSP`, mult 100, on CBOE; the real
`broker.get_option_contract` path qualifies an ATM 0DTE PUT on CBOE. Only **live 0DTE
bid/ask (fill quality)** remains — needs market hours.

**#32 replay (33 entries, now using the REAL `thesis_invalidated` rule — I found the old
replay used session-cumulative VWAP while the bot uses `ta` rolling-14, so the earlier
"N=6 −115bp / optimal" was measured on the wrong line):**

| scenario | total bp | outcome mix |
|---|---|---|
| N=6 current | **−60** | TP:1 · invalidated:**32** |
| N=6 +buf 0.05% | **−52** | EOD:10 · TP:3 · invalidated:20 |
| N=6 +buf 0.10% | −254 | EOD:14 · HARD-STOP:4 · TP:5 · invalidated:10 |
| N=6 +hold ADX≥35 | −63 | TP:1 · invalidated:32 |
| N=6 +hold ADX≥40 | −60 | TP:1 · invalidated:32 |
| N=6 +buf 0.05% +hold35 | −52 | EOD:10 · TP:3 · invalidated:20 |

Read honestly:
1. **The current rule invalidates 32 of 33 entries.** The rolling-14 VWAP *hugs price*, so
   a "recross" fires on almost everything within a few bars of entry. That's less an
   exit-patience problem than a signal that **entries are chronically on the wrong side of
   VWAP right after entry** — an entry-quality + VWAP-reference problem.
2. **The 0.05% buffer is the only knob that helps** (−52 vs −60): it converts 12 whipsaws
   into 10 EOD + 2 TP (3 winners vs 1), adds **no** hard stops. Modest and defensible, but
   still net-negative — it reduces bleed, it doesn't create edge.
3. **0.10% is too loose** (−254): 4 hard stops reappear — the 07-01 bleed. **ADX-hold does
   ~nothing** here (ADX rarely ≥35 at the invalidation moment).
4. **New lead:** the deeper lever is likely the **VWAP reference itself** — a rolling-14 VWAP
   that tracks price makes both the entry breakout and the invalidation nearly meaningless.
   An **anchored/session VWAP** (stable line) would make "beyond VWAP" and "recross" mean
   something, and ties directly to entry quality. Candid correction to the earlier "just fix
   the exit" framing: the exit knobs help at the margin; the entries + the VWAP line are the
   real problem. **Recommendation:** set `VWAP_INVALIDATION_BUFFER_PCT=0.0005` (small, safe
   win), leave ADX-hold/entry-override at 0 (no evidence yet), open an anchored-VWAP
   investigation as the next real swing.

---

## 2026-07-10 — 0W/4L 🔴 + a new close-double-fill bug

**Booked: 0 wins / 4 losses, −$267 gross.** True day P&L **unknown** — a close
double-filled and left an untracked residual SPY position (IBKR `dailyPnL` shows
+$1,074 but that's an unreliable intraday mark; reconcile after settlement).

### Trades
| Trade | Conviction | Result | Note |
|-------|-----------|--------|------|
| IWM PUT 10:17 (15-lot) | **HIGH 5/5** | −$135 | Bear trap #2 — peaked +17%, reversed, invalidation at −31% |
| IWM PUT 11:01 (re-entry) | MEDIUM 2/5 | −$24 | Invalidation −8% |
| QQQ CONDOR 11:16 | — | −$60 | Hard stop — range broke into a trend |
| SPY CONDOR 11:16 | — | −$48 (booked) | Hard stop **+ close double-filled → residual** |

### Findings

1. **🐛 NEW P0 — close over-execution (#30). → FIXED same evening** (fills-check on
   dead orders, per-submission account requantification, over-close halt, real error
   codes; 7-scenario unit test in `scripts/test_close_integrity.py`). The SPY condor close was reported
   `Cancelled` (with a misleading `10349` "TIF set to DAY" info code), so the bot
   reverted to ACTIVE and re-submitted — but the original had actually filled, so it
   **bought back 12 lots instead of 6**, leaving a **net-long 6-lot residual** that's
   now untracked. This is the "SPY untracked position again." #21 doesn't cover it
   (not a false-drop); #26 bounded the loop but a cancelled-but-filled order still
   double-fills. **Fix: reconcile the close against the actual account position —
   close only the real remaining qty, and verify a `Cancelled` order truly had no
   fill (via permId/`fills`) before resubmitting.** The `10349` red herring: capture
   the real rejection code, not the last log line. → **ACTION: user flatten the
   residual SPY condor manually.**
2. **🐛 Dashboard crash — FIXED.** `parse_trades` treated the `RECONCILE` annotation
   row as a SELL and choked on its empty Price (`could not convert '' to float`).
   Now only `BUY`/`SELL` rows are paired.
3. **The 5/5 bear-trap pattern is RECURRING — 2 for 2.** 07-09 QQQ PUT (5/5, $0.02
   below buffered ORB) and 07-10 IWM PUT (5/5, $0.06 below) both entered on razor-thin
   breakdowns and reversed. The watch-pattern logged yesterday now has two instances.
   Promoting toward action: **cap size (or skip) when the breakout margin is thin**,
   and/or widen `ORB_BREAKOUT_BUFFER_PCT`. Conviction sized the 07-10 loser to 15 lots
   → −$135, the day's biggest loss.
4. **Condors run over again** — both SPY and QQQ hard-stopped as the "range" (ADX ~21,
   many crosses at 11am) resolved into a trend. Consistent with #28: condors are a net
   drag; consider `CONDOR_ENABLED=false` until the debit side is green.

### Regime note
Another day where an 11am range read became a midday trend (like 07-01, 07-08). The
condor's range thesis and the debit bear-trap both died to the same reversal.

---

## 2026-07-09 — 🐛 CRITICAL BUG DAY: reconciliation orphaned live positions

**Booked (bot-recorded) net: ≈ −$255** (−$189 gross + $66 fees) — but the real
number is worse because **the bot orphaned 3 live positions**. First condor day,
and it exposed a serious correctness bug that must be fixed before the next session.

**IBKR reconciliation (`scripts/reconcile_ibkr.py`, run AFTER settlement):**
- **TRUE all-in result = account dailyPnL −$905.99.** Bot-booked closes ≈ −$218;
  **orphaned positions settled ≈ −$688 at expiry.** Corrected in the audit RECONCILE row.
- ⚠️ **Correction:** an earlier run *before* settlement showed −$124.41 with a +$93.88
  unrealized mark, and this retro first claimed "the orphans settled as winners." **That
  was wrong** — 0DTE mark-to-market near expiry is unreliable; only *settled* account
  P&L is truth. At settlement the orphans cost ≈ −$688, not +$94. Lesson: **run the
  reconcile AFTER settlement, never trust the intraday mark.**
- The reconciliation bug (#21) therefore cost **~$600–700 of real money** on this day
  alone — the strongest case for the fix that shipped the same evening.
- **Commissions: $100.98** — the 13-lot HIGH-conviction QQQ PUT (held **1 minute**)
  cost ~$40 alone; 1.5× sizing multiplied the drag. GO_LIVE Gate-2 fee problem, extreme.
- The permId-keyed reconcile flagged **21 orphan IBKR orders** with no audit row and
  the audit-internal check caught SPY×1 / IWM×1 opened-never-closed — two independent
  detectors that would have surfaced this within the hour had they existed intraday.

### The bug (points 1–3): position reconciliation false-positives

`_position_still_open()` ([bot.py:208](../src/bot.py)) declares a position "closed
externally" if it can't find the tracked leg in `ib.positions()` — **but it has no
guard for an empty/incomplete positions list.** When `ib.positions()` returns `[]`
(feed hiccup, subscription gap, or just not-yet-repopulated), the loop finds nothing
→ returns `False` → counts as a "miss." Two misses → the bot drops a **live**
position from tracking and fires a phantom "⚠️ closed externally" alert.

Made worse on 07-09 by: **fast-poll** (15s loops → 2 misses in 30s, not 2 min) +
**longer holds** (positions lived past the 90s grace) + real-time data switch.

**Proof from the audit — the duplicate IWM condor:**
- 11:08 — BUY IWM condor #1 (0.32 credit). **No SELL row exists.**
- 11:42 — BUY IWM condor #2 (0.16 credit) — *impossible unless #1 was dropped from
  `active_trades`* (one-trade-per-symbol rule). Reconciliation phantom-closed #1.
- 11:46 — BUY SPY CALL (0.49). **No SELL row.** Also orphaned (point 3).

So two positions were silently orphaned: **IWM condor #1** (still open on the site
per user — point 2) and the **SPY CALL** (in profit, never closed — point 3). When
reconciliation drops a trade it also **cancels the resting TP**, so the SPY call
lost even its profit-taking protection. Both settled at the 4 PM expiry unmanaged.

> **ACTION FOR USER:** reconcile the IBKR statement for the 11:08 IWM 294/298 condor
> and the 11:46 SPY 750/751 call — both expired at 4 PM. IWM sat at 297.4 (inside the
> range → the condor likely kept most of its credit); the SPY call on a bullish close
> likely finished ITM. Their P&L is NOT in `audit.csv` — add it by hand.

### Point 4 — short premium into a directional afternoon

The bot sold **3 condors at 11:05–11:08** when ADX was 12–14 (dead calm). The
afternoon then broke into a trend and **ran them over**:
- QQQ condor: −$96 (breach exit fired at **−67%** — far too late; nearly the hard stop)
- SPY condor: −$53 (breached up at 11:43)

It *did* correctly flip to a directional **SPY CALL at 11:46** when the breakout
resumed — but that's the one the bug orphaned. So the bot was positioned exactly
backwards for the afternoon (short vol into a trend) *and* the one right trade
vanished. The user's read ("directional afternoon, no directional spreads, only SPY
spread lost") is exactly right — the directional trade was made and then lost to the bug.

### What worked ✅
- **Condors traded end-to-end** (first time) — entries, credit collection, breach
  exits, and one clean +$12 profitable buy-back (IWM #2). The mechanism is sound.
- **First-ever HIGH 5/5 conviction** (10:26 QQQ PUT) — though it whipsaw-lost in 1 min.
- The regime detector fired condors on a genuinely calm 11am (ADX 12–14, 9–11 crosses).

### Findings → actions
1. **[P0-CRITICAL, #21] Reconciliation false-positive** — fail-open on empty
   positions; check *any* leg (not just the wing call); make the miss counter
   time-based (~3 min), not loop-based, so fast-poll can't accelerate false drops;
   don't reconcile during fast-poll. **The feature meant to prevent orphans is
   causing them.** Fix before next run.
2. **[P1, #22] Condor breach exit fires too late** — QQQ exited at −67%, not the
   intended ~−25%. The 2-consecutive-*closes* rule lags a fast breakout by minutes.
   Consider intrabar breach (price *touches* beyond short strike) or a tighter stop.
3. **[watch] Condors are short-vol** — a calm morning that turns directional is
   their worst case. 07-09 is the counter-example to the "5 of 6 days were
   premium-seller days" thesis: some range days *become* trend days.

### Running totals (bot-booked, gross — excludes orphans)

| Day | Regime | Net | Record |
|-----|--------|-----|--------|
| 06-30 | Trend | +$645.50 | 5W/0L |
| 07-01 | Trend → reversal | −$305.00 | 2W/3L |
| 07-06 | Flat chop | −$131.00 | 1W/3L |
| 07-07 | Bearish chop | +$14.00 | 2W/2L |
| 07-08 | V-reversal | −$156.00 | 2W/5L |
| 07-09 | Range → trend + BUG | **−$905.99 settled** (IBKR dailyPnL; ~−$688 was orphaned positions) | 1W/3L + 3 orphans |
| **Cumulative** | | **deeply negative — the 07-09 bug day dominates** | |

---

## 2026-07-08 — V-reversal day 🔴 (−$156 gross / −$210 net of fees) — the entry-selection lesson

**2W/5L, all seven exits via invalidation.** SPY: 741.3 → **739.6 low (11:31)** →
**745.7 (13:31)** — morning dip, churn at the low, strong afternoon rally. The
worst regime for breakout-following: the bot faded the V all day (5 PUTs into the
bottom, 2 CALLs knocked out by the turn's first pullback). First day with
commissions + fill-confirmed exits in the audit.

### Ledger (times ET)

| # | Trade | Held | Gross | Fees | Peak | Conviction | Exit |
|---|-------|------|-------|------|------|------------|------|
| 1 | IWM PUT 10:44 | 10m | +$13 | $13.87 (**net −$0.87**) | 20.4% | MEDIUM 3/5 | Invalidation |
| 2 | SPY PUT 10:52 | 3m | −$70 | $12.46 | 0% | MEDIUM 3/5 | Invalidation |
| 3 | IWM PUT 11:19 | 17m | +$16 | $6.32 | 25.6% | LOW −1/5 | Invalidation |
| 4 | QQQ PUT 11:29 | 9m | −$39 | $5.41 | 16.3% | LOW 0/5 | Invalidation |
| 5 | SPY PUT 11:31 | 7m | −$43 | $5.05 | 0% | LOW −1/5 | Invalidation |
| 6 | SPY CALL 13:31 | 8m | −$21 | $6.43 | 2.4% | LOW −2/5 | Invalidation |
| 7 | QQQ CALL 13:39 | 2m | −$12 | $4.98 | 0% | LOW −2/5 | Invalidation |

### The "should we have held longer?" question — answered by the data

Half yes, half catastrophic-no. **Whipsaw exits (held longer = winner):** #2 exited
at −25% 35 min before SPY hit the day's low (that PUT would have gone +50%+);
#6/#7 CALLs were cut by a 3-bar wiggle just after the turn, into a rally.
**Rescues (held longer = disaster):** #4/#5 entered PUTs *at the bottom* —
invalidation at −28/−32% was the only thing between them and −70% hard stops.
Loosening the exit is not the fix. **All 7 entries fired at 12–33 VWAP crosses** —
the tape component said "don't" while raw signal conditions were technically met.
This was an entry-selection failure, not an exit-timing failure.

### What the new instrumentation revealed

1. **Conviction gradient = damage control, confirmed.** Losses shrank monotonically
   with score: MEDIUM −$70 → LOW −$43/−$39 → LOW−2 −$21/−$12. Flat sizing ≈ −$300+.
2. **→ Hypothesis #9: skip entries when conviction score < 0.** Trades at −1/−2
   went 0W/3L (plus one −1 winner earlier); penalties exceeding all positives means
   the regime has already been proven hostile. Also fixes the fee floor (below).
3. **The fee floor is real:** $54.52 total fees (26% of the day's gross loss);
   trade #1 *won* gross and lost net; #7 paid 41% fee overhead. Tiny LOW-tier
   positions structurally can't clear friction — below some conviction the right
   size is zero, not half.
4. **Fill slippage now visible (thanks #14):** decision-vs-fill gaps up to ~21 pts
   (#4 decided at −6.5% mid, filled −28.3% — ~$0.10 crossing cost on a $0.46
   spread). Fees + slippage together ≈ $85–100/day at this trade count. The lever
   is trade COUNT, same direction as #9-skip.
5. **→ Hypothesis #10: profitable invalidations shouldn't count toward the
   throttle.** IWM PUT was stood down after two *winning* exits (+$13, +$16) —
   that signal wasn't proven wrong, its exits were just early.
6. Fast-poll (#13) never engaged — no trade reached the +35% arm zone. Unjudged.

### Regime scoreboard — the strategic takeaway

6 meaningful days: **1 clean trend (+$645), 5 chop/reversal variants (−$305, −$131,
+$14, −$156, and old-rules +$13).** The debit-breakout strategy monetizes ~1 day in
6; the guards have cut chop losses ~2× but can't make chop profitable. This is the
strongest evidence yet for the regime-matched credit-side build (TODO #9): 5 of 6
days were premium-seller days.

### Running totals (bot-closed, gross)

| Day | Regime | Net | Record |
|-----|--------|-----|--------|
| 06-29 | Chop (old exits) | +$13.00 | scratches |
| 06-30 | Trend | +$645.50 | 5W/0L |
| 07-01 | Trend → reversal | −$305.00 | 2W/3L |
| 07-06 | Flat chop | −$131.00 | 1W/3L |
| 07-07 | Bearish chop | +$14.00 | 2W/2L |
| 07-08 | V-reversal | **−$156.00** (−$210.52 net) | 2W/5L |
| **Cumulative** | | **+$80.50 gross** | fees now tracked |

---

## 2026-07-07 — Bearish chop day 🟢 (+$14) — first PUT day; conviction sizing + throttle live

**Bot realized: +$14.00 (2W / 2L).** First green chop-adjacent day ever. All four
trades were PUTs — the first bearish signals in the bot's history, proving the PUT
path end-to-end (entries, BAG orders, exits). First day fully on the refactored
modules and with conviction sizing + the invalidation throttle in production.

### Ledger (times ET)

| # | Trade | Entry | Exit | Held | P&L | Peak | Conviction | Exit |
|---|-------|-------|------|------|-----|------|------------|------|
| 1 | SPY PUT | 10:19 @0.377 | 10:48 @0.45 | 29m | **+19.3% / +$51** | 48.5% | MEDIUM 3/5 | Invalidation (profitable!) |
| 2 | QQQ PUT | 10:20 @0.39 | 10:37 @0.53 | 17m | **+35.9% / +$98** | 57.7% | MEDIUM 3/5 | Trail after +50% peak |
| 3 | IWM PUT | 10:44 @0.45 | 10:47 @0.305 | 3m | **−32.2% / −$87** | 0% | MEDIUM 2/5 | Invalidation |
| 4 | SPY PUT | 12:09 @0.35 | 12:14 @0.23 | 4m | **−34.3% / −$48** | 0% | **LOW 0/5** | Invalidation |

After #4, SPY PUT hit 2 invalidation exits → **⛔ throttle fired** (first time),
standing SPY PUT down for the rest of the day.

### What worked

1. **Conviction sizing earned its keep on day one.** The 12:09 SPY PUT re-entry
   scored **LOW 0/5** (`tape✗(22x)` — 22 VWAP crosses — plus the `inv−2` penalty)
   → $150 budget → 4 contracts. The −34% loss cost **−$48 instead of ~−$96** at
   flat sizing. The score read the regime correctly in real time.
2. **All three symbols agreed bearish at the open** (`agree✓(QQQ+IWM)`) — the
   cross-symbol component confirming a genuine down-tape, and the two agreeing
   morning entries were both winners (+$149 combined).
3. **Throttle fired correctly — and the counterfactual cleared it.** After the
   12:14 exit SPY *rallied* to ~749.3 and stayed above the buffered ORB low
   (~747.50) until ~14:45, so no PUT signal could have re-fired anyway; the only
   window was a ~15-min sliver before the 15:00 entry cutoff into a fade that
   bounced back. Verdict: the throttle cost $0 today, and both invalidation exits
   were vindicated (holding the 12:14 PUT would have hard-stopped at −70%).
   TODO #12 (2-hour stand-down) gains no urgency from today — identical outcome
   either way.
4. **Refactor survived production day 1** — all alerts, audit columns (Conviction
   populating), exits, throttle, sizing worked on the new module layout.
5. **Chop-day P&L trend: −$305 → −$131 → +$14.** The guard stack is converging.

### Watch-items

1. **→ New hypothesis #8: exit-fill sampling slippage.** QQQ's trail threshold was
   +51.9% but it filled at +35.9% — the 60-second loop sampled after a fast drop,
   giving back ~16 extra points (06-30's gaps were only 2–5 pts). On fast days the
   60s heartbeat is the exit's weakest link. Candidate fixes: poll armed trades
   faster (e.g. 15s once peak ≥ +50%), or attach a native IBKR stop order at the
   threshold. → TODO #13.
2. **Invalidation losses aren't small on violent bounces:** IWM −32% in 3 minutes,
   SPY −34% in 4. Still far better than −70%, but the "cut at −20/−30%" estimate
   from 07-01 is optimistic when the recross is sharp.
3. **SPY PUT #1 peaked +48.5%** — 1.5 pts shy of arming the trail — then the
   invalidation exit banked +19.3%. Fine outcome, but a near-miss of the +50%
   trigger; watch whether peaks clustering just under 50% recur (would argue for
   arming at 40–45%).
4. **No HIGH-conviction entries today** (morning slopes +0.7…+1.0 < 3, tape already
   6–8 crosses at 10:20). On a mixed day that's correct behavior — the 1.5× tier
   should be rare.

### Running totals (bot-closed trades only)

| Day | Regime | Net | Record |
|-----|--------|-----|--------|
| 06-29 | Chop (old broken exits) | +$13.00 | scratches |
| 06-30 | Trend | +$645.50 | 5W/0L |
| 07-01 | Trend → reversal | −$305.00 | 2W/3L |
| 07-06 | Flat chop (guards live) | −$131.00 | 1W/3L |
| 07-07 | Bearish chop (sizing + throttle live) | **+$14.00** | 2W/2L |
| **Cumulative** | | **+$236.50** | |

---

## 2026-07-06 — Flat chop day 🟡 (first full day with all chop guards live)

**Bot realized: −$131.00 (1W / 3L).** SPY pinned in a tight range all day; QQQ/IWM
never traded. All four trades were SPY CALLs, and **all four exits were thesis
invalidations** — the new Rule 2's first live day. Timestamps now in ET; `ADX_Slope`
and `Peak_Pct` columns populating.

### Ledger (times ET)

| # | Entry | Exit | Held | P&L | Peak | ADX slope at entry |
|---|-------|------|------|-----|------|--------------------|
| 1 | 11:32 | 11:54 | 22m | −17.5% / −$49 | 0.0% | +8.68 |
| 2 | 13:48 | 13:56 | 8m | −15.7% / −$40 | +5.9% | +5.13 |
| 3 | 14:19 | 14:30 | 11m | +4.4% / +$12 | +17.8% | +2.76 |
| 4 | 15:00 | 15:09 | 10m | −19.4% / −$54 | +6.5% | +10.01 |

### The headline: the invalidation exit did its job

A day like this under the old rules ≈ four rides toward −70% ≈ **−$500 to −$700**
(compare 07-01: three chop entries = −$566). Under the new rules: **−$131.**
Average loss per failed entry fell from ~−$189 to ~−$48. That's the whole point
of Rule 2, validated on day one.

### What the day exposed

1. **The entry guards don't stop this chop pattern.** SPY ground sideways *just
   above* its ORB high (ORB 747.41–749.54; SPY traded 750–752 all afternoon).
   Every poke above VWAP re-fired the CALL signal, and the churn itself produced
   "rising" ADX slopes (+2.8 to +10.0). The slope gate + buffer are calibrated for
   07-01-style fading breakouts, not for grind-above-the-range days. The exit
   contained it — but four re-entries into the same failing signal is death by
   moderate cuts.
2. **→ New hypothesis #7: invalidation-aware entry throttle.** After N (e.g. 2)
   thesis-invalidation exits on the same (symbol, direction) in one day, that
   signal has been proven chop — stand down on it until tomorrow (or double the
   cooldown). Would have cut today's loss to ~−$89 and 07-06-style days generally.
3. **No QQQ/IWM trades = mostly correct behavior.** IWM raw signals fired
   10:07–10:24 ET but the slope gate blocked them (ADX falling −0.1 to −9.9);
   IWM then stayed flat all day (user-confirmed) — **the gate's first confirmed
   save.** QQQ's ~1% morning move was never eligible: the strategy structurally
   cannot enter before ~10:00 ET (30-min ORB + ~29-bar ADX warmup), so moves that
   happen inside the opening range or as a gap are invisible to it by design.
4. **Boundary quirk:** trade #4 filled at 15:00:01 ET — submitted just before the
   15:00 entry cutoff, filled just after. Legal but ugly; it lost −19%. Consider
   moving the cutoff earlier for chop days or accounting fill latency.
5. Rule 3 note: trade #3 peaked +17.8% and exited via invalidation at +4.4% —
   invalidation also acts as a soft profit-protector below the +50% trail trigger.

### Running totals (bot-closed trades only)

| Day | Regime | Net | Record |
|-----|--------|-----|--------|
| 06-29 | Chop (old broken exits) | +$13.00 | scratches |
| 06-30 | Trend | +$645.50 | 5W/0L |
| 07-01 | Trend → reversal | −$305.00 | 2W/3L |
| 07-06 | Flat chop (guards live) | **−$131.00** | 1W/3L |
| **Cumulative** | | **+$222.50** | |

Chop-day loss shrank from −$305 (07-01, no guards) to −$131 (07-06, guards) despite
07-06 being an *entirely* signal-hostile day. Direction is right; entry-side
throttling is the next lever.

---

## 2026-07-01 — Morning trend, midday reversal ❌ (first chop stress-test of the new exits)

**Bot realized: −$305.00 (2W / 3L).** The day we said we were waiting for: morning
trend banked two clean winners, then the tape reversed midday and three positions
rode all the way to the −70% hard stop. No orphans (restart was pre-market); every
BUY has a matching SELL. *(No trades 07-02; 07-03 was the observed July-4 holiday.)*

### Ledger (times CDT)

| # | Sym | Entry (time, ADX) | Exit | Result | Exit trigger | ADX entry→exit |
|---|-----|-------------------|------|--------|--------------|----------------|
| 1 | IWM CALL | 09:02, 26.6 | 09:14 | **+$132** ✅ | trail (peak 54%) | 26.6 → **44.3 rising** |
| 2 | SPY CALL | 09:08, 25.3 | 10:08 | **+$129** ✅ | trail (peak 51%) | 25.3 → **29.7 rising** |
| 3 | QQQ CALL | 10:24, 25.5 | 11:57 | **−$185** ❌ | hard stop −71% | 25.5 → **20.3 falling** |
| 4 | IWM CALL | 10:39, 25.7 | 12:34 | **−$192** ❌ | hard stop −74% | 25.7 → **15.6 falling** |
| 5 | SPY CALL | 12:00, 29.7 | 13:55 | **−$189** ❌ | hard stop −71% | 29.7 → **21.0 falling** |

### Learnings

1. **ADX slope predicted every outcome, 5-for-5.** Rising after entry → winner;
   collapsing → loser. Entry-time *recent* slope is the tradeable proxy (Backlog #6,
   TODO #1). First hard evidence for the idea.
2. **All three losers died with the entry thesis already invalidated.** At each
   hard-stop exit, price was back **below VWAP**. Entries are indicator-based but
   exits are only P&L-based — the bot held invalidated trades for ~1h to −70%.
   A VWAP-recross exit would have cut them near −20/−30% (~$350 saved). → Backlog #5.
3. **R:R asymmetry quantified.** Winners ~+45–48% (trail construction caps them),
   losers −71/−74%. Breakeven win rate ≈ 61%. Since the exit fix: 7W/3L (70%).
   → Backlog #3.
4. **Time-of-day:** winners entered in the first ~40 min; losers were all midday
   entries (11:24 ET, 11:39 ET, 13:00 ET) at marginal ADX ~25.5. The midday
   breakout poke is the classic 0DTE trap window.
5. **Cross-symbol agreement is not sufficient** — this was an all-CALL agreement
   day that reversed anyway. Counter-evidence recorded against Backlog #2.
6. **Possible stale-data anomaly:** 12:00 SPY BUY logged ADX/VWAP/underlying
   identical to values from ~2h earlier (3 exact matches). Watching. → Backlog #4.

### Running totals (bot-closed trades only)

| Day | Regime | Net | Record |
|-----|--------|-----|--------|
| 06-29 | Chop (old broken exits) | +$13.00 | 2W/4L-ish (scratches) |
| 06-30 | Trend | +$645.50 | 5W/0L |
| 07-01 | Trend → reversal | −$305.00 | 2W/3L |
| **Cumulative** | | **+$353.50** | |

---

## 2026-06-30 — Clean bullish trend day ✅

**Bot realized: +$645.50 (5/5 wins).** Plus 2 positions orphaned by a mid-session
restart that were closed manually (P&L not captured by the bot).

### Ledger (bot-closed trades)

| # | Sym | Entry | Exit | Peak | Exit | $ P&L | Exit trigger |
|---|-----|-------|------|------|------|-------|--------------|
| 1 | QQQ | 0.52 | 0.73 | 53.9% | +40.4% | +105.00 | trail after +50% peak |
| 2 | SPY | 0.52 | 0.755 | 51.9% | +45.2% | +117.50 | trail |
| 3 | QQQ | 0.44 | 0.66 | 56.8% | +50.0% | +132.00 | trail |
| 4 | IWM | 0.417 | 0.61 | 52.2% | +46.2% | +135.00 | trail |
| 5 | SPY | 0.48 | 0.74 | 64.6% | +54.2% | +156.00 | trail |

Orphaned (opened, not bot-closed): QQQ @0.54 (re-entry #3), IWM @0.24 (re-entry).

### What worked

- **Exit-rule fix validated.** Every exit fired the new "trail after +50% peak"
  rule and locked in 40–54%. This is the single biggest lever vs yesterday
  (+$645 today vs +$13 yesterday on similar entries but the broken rule).
- **Clean trend entries.** Strong ADX all day (50.8, 39.3, 32.3, 31.1…), all
  CALLs, all aligned with a bullish tape. VWAP + ORB + ADX did its job.
- **Cooldown re-entries compounded the trend.** QQQ traded 3 legs (+105, +132,
  +open), SPY 2 legs (+117.5, +156). The 30-min cooldown + re-entry logic let
  winners ride the trend in segments.

### Learnings / watch-items

- **~8.7 pt average give-back** from peak — trend day left some on the table
  (Backlog #3).
- **3 symbols = one correlated bet.** Every trade was a CALL; QQQ/SPY/IWM rallied
  together. Great today (3× profit), dangerous on a reversal (3× loss). Consider a
  total-exposure cap across correlated symbols before live.
- **Late-day cheap re-entry is a lottery ticket** — IWM re-entry at $0.24 (1:36 PM
  ET) is a low-delta flyer. Consider raising `MIN_SPREAD_COST` or tightening
  final-hour entries.
- **Restart orphaned 2 positions** (Backlog #1).

### Caveat

5 trades, one clean trend day, 100% win rate — **do not over-fit.** Today's peaks
were all 50–65%, so even the old broken rule would've done OK; the fix's real edge
shows on marginal trades. The strategy still needs a **choppy day** to be stress-tested.

---

## 2026-06-29 — Choppy / rotational day ⚠️ (broken exit rule)

**Net: +$13 across 6 closed trades** — death by a thousand cuts. Ran under the old,
since-removed exit rule (`profit ≤ max × 70%` with no minimum-peak gate).

- The broken rule **scratched winners and ejected at losses**: IWM peaked +24.4% →
  exited +9.8%; IWM peaked +5.3% → exited **−13.2%**; QQQ peaked +8.7% → exited −2.9%.
- **Mixed signals across symbols** were the tell: IWM fired PUT (bearish) at ~10:22
  while QQQ fired CALL (bullish) at ~11:03 — symbols disagreeing = a rotational,
  choppy tape. This is the origin of Backlog #2.
- Also **left a QQQ position orphaned** (14:52 BUY, no matching SELL) — same restart
  issue as the 30th (Backlog #1).

This day is *why* the exit rule was rewritten to the current two-rule model
(hard stop −70%; trail only after +50% peak). See
[HOW_IT_WORKS.md](HOW_IT_WORKS.md#active-trade-exit-rules).
