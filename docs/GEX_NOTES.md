# GEX Forward-Test Notes & Hypotheses

A running notebook of **dealer-gamma (GEX) structural observations** and the hypotheses they
suggest — kept separate from the trade journal ([RETROSPECTIVE.md](RETROSPECTIVE.md)) because this
is about *reading the chain*, not booking P&L. **GEX has no backtest**, so this file *is* our
evidence trail: we forward-test the ideas here as the dataset grows.

> ⚠️ **The sample is tiny.** As of 2026-08-18 we have **2 GEX trades**. Everything below is a
> **hypothesis to watch, not a rule to code**. Two data points can (and do) contradict each other.
> Append new days at the top of "Daily observations"; promote/kill a hypothesis only on a real sample.

---

## Hypotheses under test

### H1 — Distance-to-flip predicts chop
**Idea:** entering *near* Gflip (net gamma ≈ 0 → no directional hedging pressure) = chop; entering
*deep* in a regime = follow-through.
**Evidence (n=2): ANTI-predictive so far.** 08-17 entered −0.055% from the flip (right *on* it) and
**won**; 08-18 entered −0.98% (deep negative gamma, the "good" distance) and **lost**. Points the
wrong way.
**Status:** parked — just collecting. `data/gex/regime_YYYY-MM-DD.csv` logs spot/Gflip/distance every
~5 min all day (in *and* out of position), so we'll have a real distribution soon.

### H2 — Chain structure predicts direction
**Idea:** a put-heavy / net-negative-GEX chain "supports a downward move."
**Evidence (n=2): NOT predictive.** 08-18 was the *more* bearishly positioned book (put/call OI **2.29
vs 0.70**, net GEX far more negative, spot 62 pts below flip) — the stronger "down" setup of the two —
and price went **up**, PUT lost. 08-17's *less* put-heavy chain → down → won.
**Status:** GEX structure is a **prior, not a forecast**. The same heavy-negative strike can *accelerate*
a breakdown **or** act as *support* (bounce) — "negative gamma amplifies" and "put wall = support" pull
in opposite directions, and which one wins isn't readable from the structure alone.

### H3 — Entry position vs the support LADDER (the "runway" refinement) — most promising so far
**Idea:** what matters isn't a single wall but **where the entry sits relative to the whole ladder of
negative-GEX (put-support) strikes.** Entry *above* the ladder = **runway** to fall *into* it (good for
a PUT); entry *below/inside* the shelf = **no runway** (bad — you've entered directly into support).
**Evidence (n=2): SUPPORTS this** — the cleanest read we have.
- **08-17 (won):** entry **7773** sat *above* a **4-deep** support ladder (7755 / 7750 / 7745 / 7740 /
  7720) → genuine runway → spot dropped **into** that zone (~7758) → PUT paid.
- **08-18 (lost):** entry **7697** sat at the **bottom of a 2-deep support shelf** (7720 −3,574M / 7700
  −3,458M, both *at/above* the entry) → **no runway** → spot reverted **up** toward the walls → PUT bled out.

**Important nuance — "breached" vs "overhead":** distinguish walls price *fell through* from walls that
sat *overhead and were never traded*.
- On 08-18 the opening range was **7698.76–7713.95**. **7720 sits above the OR high (7714)** — spot
  never traded up there all day, so it could **not** have been "breached" by falling through it; it was
  **overhead support**. **7700 *was* genuinely breached** — spot opened ~7714, ranged down, and broke
  the OR low (7698.76) to reach 7697, falling *through* 7700 on the way. So **only one of the two walls
  was breached by price action, not both** — and the entry landed *just below 7700 with the heavier 7720
  wall still overhead*, i.e. wedged in the middle of a double support shelf rather than clear below it
  with room to run. That's a structurally weak spot for a PUT to continue.

**Bucketing (now captured — `Setup_Tag`):** each GEX entry is auto-tagged into one of two buckets so we
can group and compare later:
- **`Runway`** — the lead support/resistance strike is *past* the entry (a PUT with heavy support *below*
  spot, a CALL with heavy resistance *above*) → room to run into it.
- **`IntoWall`** — entered *into/against* it (support at/above a PUT's entry) → no runway.

So far (real trades): **08-17 `Runway` +$877 · 08-18 `IntoWall` −$800 · 08-19 `IntoWall` −$1,115 · 08-20
`Runway` +$810 · 08-21 `Runway` +$590 · 08-24 `Runway` −$1,190 (2 legs).** **Real tally: `Runway` 3-2
(+$1,087), `IntoWall` 0-2 (−$1,915).** `Runway` **still beats `IntoWall`** ($+1,087 vs −$1,915) and is the
right *structural* filter — but **08-24 broke its unbeaten run**: a Runway CALL on a **chop day** (23-pt
range) failed, because the tag captures *position*, **not whether the day moves.** So `Runway` ≠ a green
light; you also need the day to actually move (trend/gap, not chop — see H1 near-flip). Both `IntoWall`s
bought a PUT *below* the 7720 wall (shorting into support) → −80%; the Runway wins entered with room to run
*and* on days that moved. Still small n — keep collecting; group the audit by `Setup_Tag` (and cross with
day-range / near-flip) — *that* is the test.

### H4 (candidate) — expected-move exhaustion (now being logged)
**Idea:** a breakout entered after the day has already realized most of its **IV-expected move**
(`spot·IV·√T`, the day's priced "budget") has **no room left to run** → it pokes and reverts. Metric:
`realized_range_at_entry ÷ expected_move`.
**Backtest (n=6):** winners capped at **38%/42%** (08-21, 08-20); the 08-24 losers sat at **52%** → an
X ≈ **47%** would have skipped 08-24's −$1,190 double loss **without skipping either winner.** The other
two losers (08-18 19%, 08-19 35%) sit *below* the winners — different failure modes (chop/IntoWall), so
the exhaustion filter correctly doesn't target them.
**Status:** `audit.csv` now logs **`Range_Exp_Ratio`** at every gex/thesis entry (log-only, no skip). n=1
exhaustion case + ~10-pt win/loss margin → collect a few weeks, then set X on real data. See TODO #7.
**Note vs 08-24's IV compression:** distinct signals — that was *implied* vol falling in the morning (a
"quiet day" prior); this is *realized range vs the budget* at entry (how much move is already spent).

---

## Mechanical concepts (so we don't re-derive them)

- **Gflip / regime.** Spot < Gflip → **negative gamma**: dealers hedge *with* the move (sell into
  weakness) → amplify → momentum-friendly. Spot > Gflip → **positive gamma**: dealers fade → dampen → chop.
- **Ladders (`gex.gex_ladders`)** = the top-3 **gamma-weighted** heaviest resistance (most-positive GEX)
  and support (most-negative GEX) strikes. This is the "GEX wall" external providers quote — **distinct
  from `concentration_zones`** (raw-OI top strikes), which is what the bot's *wall-breakout entry* uses.
  The audit's `Call_Ladder`/`Put_Ladder` columns are the gamma-weighted top-3.
- **Our net-GEX $ is our-own-convention** (±5% / 3-expiry / 50-strike window, $ per 1% move). It is
  **internally consistent** for our day-to-day comparison but **not comparable** to a provider's
  full-chain $ figure — don't line our −37,629M up against their "$38M".
- **Cross-check that our math is sound:** our `gex_ladders` independently put 08-17's heaviest put-support
  strike at **7755**, matching the external **−$40.08M @ 7755** reading — from a completely different,
  smaller chain subset.
- **What we now freeze at every order** (audit + Discord submit alert): Gflip, spot, distance-to-flip %,
  the one-word **`Regime`** (negative = spot < Gflip = momentum premise / positive = dampen; added 2026-08-25,
  all history backfilled from the dist sign), net GEX (total + 0DTE, $M), the **top-3 support/resistance
  ladders** (`Put_Ladder`/`Call_Ladder`, gamma-weighted, heaviest first), and a one-word **`Setup_Tag`**
  bucket. Historical 08-17/08-18 rows were backfilled. NB: every mechanical entry so far reads `negative` —
  by construction (the GEX signal requires spot < Gflip), so `Regime` flags a *transient* poke below the flip
  (like 08-25's 10:31 dip on an otherwise positive-gamma day) vs a sustained neg-γ break; cross-check against
  the 5-min `data/gex/regime_*.csv` for the day's prevailing regime.

---

## Daily observations *(newest on top — append here each GEX day)*

### 2026-08-24 (CALL, −$585 gex + −$605 thesis, catastrophe stop — FIRST Runway loss, a chop day)
- **Setup:** entry 7664, Gflip 7681 (−0.22%), Setup_Tag Runway. Both gex + thesis fired the SAME bullish CALL
  on the OR-high break (7663). Day was a **23-pt chop** (7645–7668, flat close) → the "breakout" was a poke to
  the range high that reverted. Peaked +40%, rode to −80%.
- **First `Runway` LOSS** (was 3-0). Lesson: the tag captures the structural *position*, NOT whether the day
  MOVES. **Runway + chop = loss.** Past Runway wins were all on days that moved (trend/gap); today chopped.
- **H1 (near-flip → chop):** entry −0.22% from the flip on a 23-pt range → another near-flip chop point. BUT
  08-21 was ALSO −0.22% and won +90% (it moved). So near-flip caps the *ceiling*; chop-vs-move decides.
- **IV check (per user Q):** ATM 0DTE IV flat ~11% *during* the hold (no crush) → the −80% was **spot reversal
  + theta**, not IV. But IV compressed **14.5%→11% in the morning** (a "quiet day" signal our *realized*-vol
  filter missed). Candidate: an *implied*-vol chop filter. n=1.
- **Trail-arm lowered 0.50 → 0.35** (08-19 +28% and 08-24 +40% both gave modest peaks back to −80%; the arm
  only gates whether the trail is active, so it protects modest peakers without touching big winners). TODO #38 done.

### 2026-08-21 (mechanical GEX did NOT trade — near the flip, chop; the thesis CALL won +$590)
- **Regime: NEAR the flip, not deep neg-γ.** Gap up from 7642 → spot ~7679 vs Gflip **7695.91 = −0.22%**
  (vs −1%+ the prior days). The mechanical GEX formed OR-breakouts all day but **skipped every one**
  (no-momentum gate + low-vol gate) → **zero GEX trades.** The filters did their job on a chop day.
- **H1 data point (distance-to-flip → chop):** entered the session −0.22% from the flip and the tape
  **chopped ~75 min** (7660–7673) before breaking — consistent with "near the flip = little hedging
  pressure = chop." One more brick for H1 being about *chop timing*, even if it's weak as a *direction* signal.
- **The winner was the thesis rail, not GEX** — a `Runway` CALL (entry above the 7640–7665 shelf, air
  pocket to the flip). See [RETROSPECTIVE.md](RETROSPECTIVE.md) 08-21. GEX real tally unchanged at −$228.

### 2026-08-20 (PUT, +$810, trailing stop — first `Runway` win the SYSTEM captured end-to-end)
- **Setup:** entry 7675, Gflip 7728 (−0.69%, deep neg-γ), broke below OR-low 7679 w/ 2-bar down-accel.
  Put ladder `7650|7640|7645` (all *below* the entry) → **`Runway`**. Spot fell 7675 → 7642 (day's low),
  right into the support zone. Peaked +79%, trailing stop booked **+63% = +$810**.
- **The clean H3 test:** the *same instrument* that lost twice last week (a GEX PUT in deep neg-γ) **won**
  today — the only difference was the tag. 08-18/19 = `IntoWall` (PUT below the 7720 wall, into support,
  bounced up); 08-20 = `Runway` (PUT above the 7650 ladder, room to fall, followed through). `Runway` 2-0,
  `IntoWall` 0-2, 3-for-3.
- **Trailing stop's first real win** — no TP cap, let it run to +79%, gave back 20% to book +63%. The
  "convex tail" design working as intended (vs 08-19: peaked +28%, never armed, rode to −80%).
- **H1 footnote:** entry was −0.69% from flip ("deep" neg-γ) and **won**; 08-18 was also deep (−0.98%) and
  **lost**. Distance-to-flip doesn't separate them — the `Setup_Tag` does. Consistent with H1 weak, H3 the signal.
- **Thesis rail (first live day):** the bullish CALL arm (break >7716) **correctly never fired** — the day
  was bearish, spot never got within 20 pts of the trigger, arm expired 15:55. The wrong human read cost $0.

### 2026-08-19 (PUT, −$1,115, catastrophe stop — 2nd straight `IntoWall`) + a `Runway` CALL thesis that would've won
- **Bot trade:** PUT @ 13.80, entry 7706, Gflip 7732.8 (−0.34%), put ladder `7720|7685|7650` → **`IntoWall`**
  (bought *below* the 7720 wall). Peaked +28% at the 7700 low, never armed the trail, rode to −80%.
- **Same mistake as 08-18** (entry below 7720, shorting into support). Price bounced off ~7700, rallied to
  7744 → the PUT was on the wrong side *and* into support.
- **Contrast (H3):** the day's move was UP; a `Runway` CALL (entry ~7722 above the pivot, resistance
  7725/7750 to run to) simulated **+$947** with the trailing exit. So today: `IntoWall` PUT −$1,115 (real)
  vs `Runway` CALL +$947 (sim) — the tag pointed the right way on *both* sides.
- **Read:** `IntoWall` = 0-2 real, both catastrophe-stopped. Buying a PUT below the heavy put-support strike
  is shorting where dealers defend — price keeps bouncing back up (the magnet from H2/H3).

### 2026-08-18 (PUT, −$800, auto-closed on the −80% catastrophe stop)
- **Setup:** entry 7697, Gflip 7768 (−0.98%, deep neg-γ), put/call OI 2.29 (heavily put-heavy), net GEX
  total −37,629M / 0DTE −23,432M. The *textbook* GEX setup — and it **lost**.
- **Support shelf 7720/7700** (H3): entry landed *inside* the shelf (7700 breached, 7720 overhead) → no
  runway → spot chopped 7696–7710 around the walls and never broke lower → PUT decayed to −80%.
- **Read:** today the walls acted as **support** (price held), not as a breakdown accelerant. Feeds H2
  (structure ≠ forecast) and H3 (entered into support, no runway).

### 2026-08-17 (PUT, +$877, manual close — bot couldn't close, tick bug since fixed)
- **Setup:** entry 7773, Gflip 7777.5 (−0.055%, *on* the flip), put/call OI 0.70, net GEX total −3,721M /
  0DTE −5,872M, heaviest put-support strike **7755** (matches external −$40.08M).
- **Support ladder** (H3): entry sat *above* a 4-deep ladder (7755/7750/7745/7740/7720) → runway → the
  afternoon selloff carried spot **into** the 7755 support zone (~7758) → PUT paid (peaked +100%).
- **Read:** the win came from having runway *below* the entry to fall into — the opposite of 08-18.
  Also note: the entry was marginal on distance-to-flip (H1, on the flip) yet won — one reason H1 looks weak.
