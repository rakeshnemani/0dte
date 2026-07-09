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

## 2026-07-09 — 🐛 CRITICAL BUG DAY: reconciliation orphaned live positions

**Booked (bot-recorded) net: ≈ −$255** (−$189 gross + $66 fees) — but the real
number is worse because **the bot orphaned 3 live positions**. First condor day,
and it exposed a serious correctness bug that must be fixed before the next session.

**IBKR reconciliation (`scripts/reconcile_ibkr.py`, run after expiry):**
- **TRUE all-in result = account dailyPnL −$124.41** (realized −$218.29 + orphan
  settlement +$93.88). Booked into `audit.csv` as a RECONCILE row.
- **The orphans settled as WINNERS (+$93.88).** The SPY call spread finished ITM,
  QQQ/IWM residuals settled favorably. So the bug cost *visibility and control*, not
  (this time) money — but that's luck: the 25-lot IWM residual could as easily have
  been a −$2.5k max-loss. The orphaning is unacceptable regardless of outcome.
- **Commissions: $100.98** — enormous. The 13-lot HIGH-conviction QQQ PUT (held
  **1 minute**) cost ~$40 in fees alone; 1.5× sizing multiplied the drag. This is the
  GO_LIVE Gate-2 fee problem in the extreme.
- Orphaned at peak: SPY 6-lot 750/751 call spread, QQQ 4-lot condor, and a **25-lot
  IWM residual** from repeated orphan/re-open cycles (all expired ITM/worthless per
  the account; user confirmed all showed "expired" on the site).

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
| 07-09 | Range → trend + BUG | **−$124.41 all-in** (IBKR dailyPnL, reconciled) | 1W/3L + 3 orphans |
| **Cumulative** | | **~−$57 (gross, ex-fees)** across 6 days | |

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
