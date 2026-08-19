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

So far: **08-17 = `Runway` → won; 08-18 = `IntoWall` → lost.** As the sample grows, group the audit by
`Setup_Tag` and compare win-rate / avg P&L — *that* is the test of H3. (It's still a computed **label**,
not a trading rule. A fuller "runway = net GEX below vs above the entry" number is a possible future add.)

---

## Mechanical concepts (so we don't re-derive them)

- **Gflip / regime.** Spot < Gflip → **negative gamma**: dealers hedge *with* the move (sell into
  weakness) → amplify → momentum-friendly. Spot > Gflip → **positive gamma**: dealers fade → dampen → chop.
- **Walls (`gex.gex_walls`)** = the **gamma-weighted** heaviest resistance (most-positive GEX) and support
  (most-negative GEX) strikes. This is the "GEX wall" external providers quote — **distinct from
  `concentration_zones`** (raw-OI top strikes), which is what the bot's *wall-breakout entry* uses. The
  audit's `Call_Wall`/`Put_Wall` columns are the gamma-weighted ones.
- **Our net-GEX $ is our-own-convention** (±5% / 3-expiry / 50-strike window, $ per 1% move). It is
  **internally consistent** for our day-to-day comparison but **not comparable** to a provider's
  full-chain $ figure — don't line our −37,629M up against their "$38M".
- **Cross-check that our math is sound:** our `gex_walls` independently put 08-17's heaviest put-support
  strike at **7755**, matching the external **−$40.08M @ 7755** reading — from a completely different,
  smaller chain subset.
- **What we now freeze at every order** (audit + Discord submit alert): Gflip, spot, distance-to-flip %,
  net GEX (total + 0DTE, $M), the **top-3 support/resistance ladders** (`Put_Ladder`/`Call_Ladder`, gamma-weighted, heaviest first), and a one-word **`Setup_Tag`** bucket. Historical 08-17/08-18 rows were backfilled.

---

## Daily observations *(newest on top — append here each GEX day)*

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
