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
