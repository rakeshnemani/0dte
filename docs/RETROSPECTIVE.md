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

Still open: **#2** (needs regime-flip data) and **#3** (win-side tuning — re-evaluate
after the new loss-side rules have data). The new guards themselves are now
hypotheses under test: watch for **entries the slope gate blocks that would have won**,
and **invalidation exits that would have recovered**.

| # | Idea | Evidence so far | What would confirm / kill it |
|---|------|-----------------|------------------------------|
| 1 | **Startup position discovery** | Restart orphaned open positions on **both** 2026-06-29 and 2026-06-30 — the fresh process lost `active_trades`, so it didn't manage or EOD-flatten them. (2026-07-01: restart happened pre-market, no orphans.) | This is an operational bug, not a hypothesis — do it before going live regardless of data. |
| 2 | **Cross-symbol agreement as internal breadth** | 2026-06-30: all 3 symbols CALL (agreement) → trend → +$645. 2026-06-29: IWM PUT vs QQQ/SPY CALL (disagreement) → chop → +$13. **Counter-evidence 2026-07-01:** all-CALL agreement day *reversed midday* → −$305. Agreement reads the regime *at entry time* but can't detect an intraday flip. $TICK/$VOLD still logs *"insufficient — bypassed"* on every trade — dead weight on this feed. | Refine: agreement as an entry-time filter only, paired with an intraday regime-change exit (see #5). More days needed. |
| 3 | **Exit tuning — R:R asymmetry** | Winners bank ~+45–48% (trail arms at +50%, exits at 90% of peak); losers realize −71/−74% (hard stop). **Breakeven win rate ≈ 61%.** Current record since exit fix: 7W/3L = 70% — profitable but thin. 2026-06-30 also showed ~8.7 pts give-back from peak on trend days. | Fixing the loss side (see #5) matters more than squeezing the win side: cutting losers at ~−30% drops breakeven to ~40%. |
| 4 | **Data hygiene** | `Profit_Pct` column shows `0.40%` instead of `40.38%` (missing ×100). Audit timestamps in local **CDT**, strategy runs in **ET**. **New 2026-07-01:** the 12:00 SPY BUY logged *identical* ADX (29.73), VWAP (747.43), and underlying (747.63) to values recorded ~2h earlier — 3 exact matches suggests possible stale bar data at entry evaluation. | Cleanup whenever; investigate the stale-indicator anomaly if it recurs. |
| 5 | **Thesis-invalidation exit** (price recrosses VWAP → exit) | **All three 2026-07-01 losers** had price back *below VWAP* at their hard-stop exits (QQQ 728.02 < 728.48, IWM 301.29 < 301.51, SPY 746.55 < 746.87). The entry reason ("price > VWAP and > ORB high") was long dead while the bot held to −70%. An invalidation exit would have cut them near −20/−30% — ~$350 saved on one day. | Watch: how often does price dip below VWAP *then recover* on winning trades? If rare, implement; if common, require N consecutive bars below or a buffer. |
| 6 | **Entry quality in chop** (ADX slope + time-of-day + breakout buffer) | 2026-07-01: ADX slope entry→exit predicted outcome **5-for-5** (rising → both winners; falling → all three losers). Both winners entered in the first ~40 min; all losers were midday entries (11:24 ET+) at marginal ADX ~25.5 on micro-poke breakouts. Supports the ADX-slope idea in [TODO.md](../TODO.md). | Track ADX slope at entry (last ~10 bars) + entry time for every trade. If the pattern holds across more days, gate midday entries on rising ADX / wider breakout margin. |

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
