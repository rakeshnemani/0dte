# mapAndCompassGEX — design (NOT built; evidence-gated)

**Status: DESIGN / ideation (2026-09-14).** No code yet. Build is gated on the evidence step below.
A **3rd GEX sleeve** alongside `mechGEX` (mechanical, regime-driven) and `thesisGEX` (human-in-the-loop).

## The one-line idea

**GEX is a map, not a compass.** It reliably tells us *where* price gravitates/stalls (the flip + walls —
5/5, ~1–10pt in the [[GEX_NOTES]] pre-market watch), but **not which way the day goes**. So:
- **Map (pre-open GEX):** the flip pivot + the walls above/below = the day's targets.
- **Compass (the 15-min OR break):** direction — break OR-high → CALL, OR-low → PUT. Regime-agnostic.
- **Viability gate (the new bit):** only take it if the **runway to the target wall clears the premium + fees** —
  i.e., if a *full* run to the wall would still leave us underwater, skip (we'd lose even if the direction is right).
- **Exit: convex tail, no TP** (same as mechGEX) — the wall is a *target for sizing the trade*, NOT a take-profit.
  If price blows through the wall, we ride it.

## Why it exists (vs mechGEX)

mechGEX forces GEX to be both map AND compass — it derives *direction* from the **regime** (neg-γ = go with
momentum). That's the blind spot: on positive-γ trend days (09-03, 09-11) the regime gate refuses the CALL and we
miss the move. mapAndCompassGEX **takes direction from the OR break instead**, so it works in either regime, and
uses GEX only for the target/runway — the part that's actually proven.

## Entry logic (draft)

1. **Map** — from the ~9:45 snapshot (fresh, post-open): flip pivot + nearest heavy wall above and below.
2. **Compass** — after the 15-min OR: 1-min close beyond OR-high → CALL; beyond OR-low → PUT. (Reuse the existing
   `or_breakout` machinery.) Possibly require `confirm_bars` (a held break, not a wick).
3. **Viability gate (runway vs premium)** — quote the ATM option (premium `P` points). Target wall = nearest heavy
   wall in the break direction; runway `R` = |wall − entry spot| points.
   - **Simple floor:** if `R ≤ P + fee_buffer`, skip (even full intrinsic at the wall can't clear the premium).
   - **BS projection (the real test):** price the option at the target spot via Black-Scholes (chain IV + expected
     hold time-to-expiry); if `projected_value ≤ premium + fees + margin`, skip. This is the quantified upgrade of
     the binary IntoWall tag — from "is there runway?" to "is the runway worth the premium?"
4. **Inherited gates** — keep low-vol skip (dead chop), exhaustion skip (spent move), and a **regime-known**
   requirement (Gflip must compute). IntoWall becomes subsumed by the viability gate (no-runway = fails viability).
5. **Stop / invalidation** — candidate: price falls back inside the OR (the break failed). TBD.

**Exit:** convex-tail trailing (arm +35%, tiered giveback) + −60% catastrophe + EOD flatten — **identical to
mechGEX. No take-profit.**

## The risk: chop

An OR break that fakes out and reverses (chop day) is the natural predator. We haven't *seen* it lose yet, but
that's partly survivorship — the low-vol/exhaustion/runway gates blocked the choppy entries. Mitigations: keep
those gates, require a held OR break (confirm_bars), and the OR-reclaim stop. **Chop is the thing to watch in the
evidence phase.**

## Evidence plan (before any code)

1. **Backtest the viability gate on logged trades — ✅ DONE 2026-09-14, and it separates cleanly.**
   Per trade: runway `R` = |nearest target wall in the profit direction (from the frozen ladder) − entry spot|;
   premium `P` = entry option price (points). Result on 17 logged trades:
   - **`R < P` (a full run to the wall can't even clear the premium): 0W / 3L, −$2,815** — 08-28 PUT (R/P 0.60),
     09-01 thesis PUT (0.73), 09-10 PUT (0.51, the IntoWall trade). **Every R<P trade lost.**
   - **No winner has R/P < 1** (nearest winner: 08-27 CALL at 1.44). So the gate removes *only* losers.
   - Winners cluster at high runway (R/P 1.9–6.2): 08-28 +$1,600 (1.94), 09-01 +$860 (5.55), 08-17 +$880 (2.12).
   - Keeping `R ≥ P` takes the book **−$570 → +$2,245 (8W/6L)**; `R ≥ 1.5P` starts cutting a winner (08-27),
     so the threshold sweet spot is **~[P, 1.4P]**, not higher.
   - **Why this is stronger than the exhaustion gate's fit:** the `R < P` line isn't tuned — it falls out of the
     breakeven identity (a long needs ~P points to break even; if the target is < P away, it's a math-guaranteed
     loss even when the direction is right). First-principles, not curve-fit.
   - **Caveats:** only n=3 flagged (suggestive, not proof); overlaps IntoWall (09-10 already gated → incremental
     *mechanical* catch beyond IntoWall is just 08-28 −$875; 09-01 is thesis/ungated); and this is the
     conservative expiry-intrinsic floor — the **BS-projection** version would pin the real intraday threshold
     (likely a bit above P, since delta<1 + theta mean you need R somewhat > P to actually profit).
2. **Chain+regime backtest — ⚠️ DONE 2026-09-14, RESULT CAUTIONARY (and P&L unreliable).**
   Simulated OR-break entries + BS-reconstructed convex-tail P&L across 19 days (`scratchpad/mac_backtest.py`).
   - **P&L is untrustworthy** — 0DTE ATM options reconstructed via BS on **5-min bars** are hyper-sensitive, so the
     −60% catastrophe trips on intrabar noise and the coarse path misses recoveries. Proof: **09-03 (a clear +44
     trend-up day) shows the CALL at peak 0% → catastrophe −$779** — impossible for a real CALL there. Several
     entries share that "peak 0% → catastrophe" artifact. So the printed nets (raw −$1,613; "R<P gate" +$1,077) are
     **artifacts, not results.** (Same limitation as "GEX has no backtest" — no real option prices, no fine bars.)
   - **Reliable structural findings (entry/direction only):**
     - **OR break fires 19/19 days** → the compass alone is *far too loose* (trades every day, chop included);
       needs a strong gate to be viable.
     - **The compass whipsaws** — 09-11 (a green gap-up day) broke the OR *low* at 10:42 → generated a **PUT** on a
       day that closed green. Wrong direction, structural (not a reconstruction artifact). OR-break direction is
       ~coin-flip on non-trending/reversing days — the chop risk is bigger than first assumed.
   - **Takeaway:** the idea isn't dead, but the **OR-break-as-compass is shakier than hoped** and rests on a
     coin-flip direction signal. Rethink the compass (held break / retest / gap-and-trend rather than raw OR)
     before trusting it. Confidence lowered.
3. **Prototype manually via the thesisGEX rail (zero new code)** — `arm` with an `or_breakout` trigger =
   compass; you authorize; convex-tail exit. Log outcomes for a few weeks. **Now the primary validation path**,
   since the backtest can't measure P&L and flagged the compass.
4. **If it earns its keep → build the mechanical sleeve** (log-only first, like exhaustion/IntoWall were).

## Open questions

- BS hold-time assumption for the projection (enter ~10:00, target hit when? use remaining-T-at-entry as a
  conservative bound, or a fixed 1–2h hold?).
- OR break vs OR break + retest for the compass (fewer fakeouts vs later entry).
- Does it replace mechGEX eventually, or run alongside it? (They'd overlap on neg-γ OR-break days.)
- Target-wall selection: nearest heavy wall, or the gamma-weighted peak node?
