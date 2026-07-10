# 0DTE Bot — Improvement Backlog

Items to validate or implement once the core strategy has enough paper-trade history.

See [docs/RETROSPECTIVE.md](docs/RETROSPECTIVE.md) for the daily trade journal and
evidence-backed hypotheses behind these items, and [docs/GO_LIVE.md](docs/GO_LIVE.md)
for how they map to the paper→live gates.

## Priority queue

| Priority | Meaning | Items |
|----------|---------|-------|
| **P0 — CRITICAL** | Live-money correctness bugs | ✅ **all clear** — #21 (reconciliation), #25 (permId), #26 (retry-loop) done 2026-07-09 |
| **P0 — do next** | Directly moves the fee-adjusted edge or is a mandatory safety control; small builds | *(cleared — #13 and #15 done 2026-07-07)* |
| **P1 — soon** | Required before live or user-committed next major build | **#2** exposure cap, **#16** always-on host, **#22/#28** condor tuning *(#23 hourly summary, #24 reconcile, #9 condors, #17/#18/#19 → done)* |
| **P2 — evidence-gated** | Good hypotheses waiting for data or a trigger day | **#20** wider spreads (fee-ratio experiment), **#5** time stop, **#6** midday tightening, **#7** expected-move anchor, **#12** 2-hour throttle |
| **P3 — parked / live-transition** | By design not now | **#3** XSP switch, **#4** GLD/TLT, **#8** VIX1D |

Rationale for the P0s: after fee adjustment the edge is currently negative (see
GO_LIVE Gate 2). #13 attacks the biggest recoverable leak — winners giving back
10–16 pts to 60-second exit sampling — and doubles as the Gate-4 restart-safety
requirement (native stops). #15 is ~20 lines and caps the day a guard fails.

---

## P0 — Do next

*(cleared 2026-07-07 — #13 and #15 in Done below; next up is the P1 batch)*

## P1 — Soon

2. **[P1] Total-exposure cap** — The 3 symbols are one correlated equity-beta bet (07-01: three same-direction positions, three simultaneous hard stops). Cap concurrent positions (e.g. max 2) or same-direction dollar exposure. Required for GO_LIVE Gate 5; small risk-control guard in `execute_trade`, same shape as the daily loss limit (#15, done).

16. **[P1] Always-on host** — Move the bot off the laptop (VPS or dedicated machine; interim: tmux + caffeinate). Two Ctrl+C/sleep incidents already; GO_LIVE Gate 4's "20 clean sessions" clock can't start until this is done. Operational task, not code. *(Imported from the go-live checklist.)*


## P1 / P2 — Queued & evidence-gated


28. **[P1 — reconsider] Condors may be a net-negative structure** — Cumulative through 07-09: condors 1W/2L, −$137 gross, and the $1-wide structure needs an **86% win rate** to break even (collect ~$0.30, risk $0.70). Options: (a) require much higher `MIN_CONDOR_CREDIT` / wider wings so R:R isn't so lopsided; (b) tighten the breach exit (#22) so run-overs cost less; (c) **shelve condors entirely (`CONDOR_ENABLED=false`) until the debit side is fee-adjusted-green** — don't let a second unproven structure add fee drag while the core isn't paying for itself. Decide after ~5 more condor days OR just disable now to reduce noise. Small sample — but the structural math is a red flag.

22. **[P1] Condor breach exit fires too late** — 2026-07-09 QQQ condor exited at −67% (near the hard stop), not the intended ~−25%: the "2 consecutive 1-min *closes* beyond a short strike" rule lags a fast breakout by minutes. Consider an intrabar trigger (price *touches* beyond the short strike), or reduce to 1 close, or add a tighter condor-specific stop (e.g. −40%). Needs a couple more condor days to calibrate vs. false breaches.

20. **[P2] Wider spreads to cut the fee ratio** — Fees are per contract, so raising `MAX_POSITION_SIZE` does NOT improve the commission ratio (1.67× budget = 1.67× contracts = 1.67× fees). What does: $2-wide spreads on SPY/QQQ — roughly double the premium per contract → half the contracts → **half the fees per dollar of exposure** (~4.4% → ~2.2%). Needs analysis first: liquidity at $2 widths, and whether the deeper spread's % P&L behavior changes exit-rule calibration. Run as an experiment on one symbol after #17/#19 have a week of data.

5. **[P2] Time stop** — Debit spreads bleed theta; if a trade isn't at ~+15% within 45–60 min, it's failing even if price is flat. All three 2026-07-01 losers were held 90–115 min. *Downgraded from "next batch": the invalidation exit now cuts thesis-dead trades in 3–22 min; the remaining case (thesis alive but going nowhere while theta bleeds) needs evidence it still occurs.*

6. **[P2] Midday tightening** — Require ADX > 30 (vs 25) for entries between 11:30–13:30 ET. All three 07-01 losers entered midday at ADX ~25.5. *Partially covered now: conviction sizing already down-weights midday entries (`early✗`); promote to a hard gate only if midday MEDIUM entries keep losing.*

7. **[P2] Expected-move anchor** — Price the ATM straddle at ~10:00 ET to get the day's expected move (broker helpers exist). Skip breakout entries once the day has moved >~80% of it (exhaustion filter); later, use for strike/target selection.

12. **[P2] Soften the invalidation throttle: 2-hour stand-down instead of all day** — Requested 2026-07-07; the same day's counterfactual showed no urgency (blocked signal never re-fired anyway). Implement when a real "morning chop → afternoon trend" day demonstrates the cost. `THROTTLE_STANDDOWN_HOURS=2`, `0` = rest of day; consider requiring HIGH conviction for the first re-entry after expiry.

## P3 — Parked / live-transition

3. **[P3 — live transition] Replace SPY with XSP** — Same index, but 60/40 Section-1256 tax treatment + cash settlement (no assignment risk on orphans/expiry) + European exercise. Cost: much wider bid-ask than SPY (~$0.05–0.15 vs $0.01–0.02), so validate fills with small size first. Generate signals from SPY bars (real volume → real VWAP), execute on XSP options. Do **not** trade both — 100% signal duplication. Belongs to GO_LIVE Gate 6.

4. **[P3 — experiment] GLD/TLT on their expiry days** — Only genuine diversification available; no daily 0DTE, so verify their expiration calendar + 0DTE-day option liquidity in IBKR first, then add strike/width configs.

8. **[P3] VIX1D regime filter** — Fetch `Index('VIX1D', 'CBOE')`; block or downsize entries when 1-day vol is spiking against the trade direction. Cheap fetch, but another threshold to calibrate — revisit after the conviction score's calibration pass proves the current regime signals out.

---

## ✅ Done

21. ~~**Reconciliation false-positives orphan live positions**~~ — ✅ **2026-07-09**. `_position_still_open()` rewritten: **fails open on an empty positions feed** (an account with an open 0DTE spread always shows ≥2 legs, so empty = feed-not-ready, not closed — the actual bug), checks whether **any** leg is held (all leg conIds now stored per trade, incl. all 4 condor legs), and the drop decision is **time-based (180s of consistent absence)** not loop-count so 15s fast-poll can't drop a live trade in 30s. Added an **anti-cascade entry guard**: `execute_trade`/`execute_condor` refuse to open while the account holds untracked legs for that symbol (⚠️ alert once/day), so a phantom-close can't spawn a duplicate. Unit-tested (empty-feed → still-open, any-leg → still-open, live-feed-absent → closed, entry guard). Root-causes #26 too.

26. ~~**Order reject/retry infinite loop on error 201**~~ — ✅ **2026-07-09**. `close_position()` now caps retries: attempts are spaced by a 30s cooldown, and after 4 rejections the bot sets `close_failed`, fires a 🛑 **CLOSE FAILED — MANUAL ACTION NEEDED** alert, and **stops auto-retrying** (keeps the trade tracked — never orphans it). On an error-201 rejection specifically, it sweeps stray open orders on the underlying (`cancel_open_orders_for`) before the next attempt, and logs the exact IBKR error code/message from the order log. Root cause was already removed by #21 (no orphan cascade → no conflicting orders); this bounds the loop as belt-and-suspenders. Unit-tested (4 attempts → give up + alert).

25. ~~**permId as a tracked key + in audit**~~ — ✅ **2026-07-09**. `broker.order_perm_id()` reads IBKR's permanent order id; captured as `entry_permId`/`exit_permId` on the trade and written to a new `PermId` audit column (both BUY and SELL rows). `scripts/backfill_permid.py` retro-filled today's rows by matching IBKR executions on (symbol, price, time) — 9/10 matched (older rows are outside IBKR's ~24h execution window; need Flex Query per #24). permId is the exact join key for reconciliation and audit↔IBKR.

24. ~~**Ad-hoc P&L reconciliation by date**~~ — ✅ **2026-07-09**. `scripts/reconcile_ibkr.py [date] [--write]` now: (a) auto-uses an **IBKR Flex Query** for dates older than the ~24h live-API window (`IBKR_FLEX_TOKEN`/`IBKR_FLEX_QUERY_ID` env vars — code done, needs the user's one-time Flex setup); (b) **joins on `permId`** for exact per-order matching, flagging IBKR orders with no audit row as ORPHANS, plus an audit-internal "BUY with no SELL" check; (c) **`--write`** appends orphan RECONCILE rows to the audit (backs up first). permId-join + orphan detection unit-tested offline against the real 07-09 audit (caught the unbooked order and the SPY/IWM orphans).

23. ~~**Hourly Discord health summary**~~ — ✅ **2026-07-09**. `notify_hourly_health` fires once per ET clock hour during market hours (`_last_hourly_hour` guard, reset daily): awaiting-fill / open (live P&L + peak) / closed-today + running net, and flags any `close_failed`. Liveness heartbeat so orphans/stuck orders surface within the hour, not at EOD.

29. ~~**Condor setup-quality in alerts**~~ — ✅ **2026-07-09**. Condors don't use the directional conviction score (every component is inverted for range trades — a condor wants low ADX + many VWAP crosses). Instead the ⏳/🦅 condor alerts now show a `Setup quality` line — credit/width ratio (the R:R), ADX, and VWAP-cross count — with a note that condors are sized by max-loss budget, not conviction.

27. ~~**Operational logging (separate from audit)**~~ — ✅ **2026-07-09** (`src/logging_setup.py`): all bot activity (orders, IBKR errors, reconnects, exit decisions, reconciliation drops, dashboard rebuilds) now written to `logs/bot.log` — daily rotation, 30-day retention, ET timestamps, with module names. Console output unchanged. Distinct from `audit.csv` (financials only). Motivated by the 2026-07-09 error-201 loop scrolling off the terminal. *Tuning knob: ib_insync INFO `placeOrder` dumps are captured for order-debugging; drop them to WARNING if the file gets noisy.*

1. ~~**ADX slope check (rising vs. flat)**~~ — ✅ **2026-07-05** as `ADX_SLOPE_BARS=10` (entry requires ADX rising over the last 10 bars; fails open early-session).
   *Evidence 2026-07-01: ADX direction (entry→exit) predicted all 5 trade outcomes.*

10. ~~**Invalidation-aware entry throttle**~~ — ✅ **2026-07-06** as `MAX_INVALIDATIONS_PER_SIGNAL=2`: after 2 thesis-invalidation exits on a (symbol, direction) in one day, the signal stands down until tomorrow. ⛔ Discord alert on trip. First fired 2026-07-07 (SPY PUT); counterfactual cleared it.

11. ~~**Conviction-based position sizing**~~ — ✅ **2026-07-06** (both tiers live per user — paper trading): score 0–5 (+1 each: ADX ≥ 30, slope ≥ +3, cross-symbol agreement, pre-11:00 ET entry, ≤ 4 VWAP crosses; −1 per invalidation today) → LOW 0.5× / MEDIUM 1× / HIGH 1.5× budget. Logged to audit, shown in Discord, charted in dashboard. **Calibration pass pending** (~2 weeks of `Conviction` column data). First live save 2026-07-07: LOW sizing halved a losing re-entry.

13. ~~**Fix exit-fill sampling slippage**~~ — ✅ **2026-07-07** via fast polling: the loop drops 60s → `FAST_POLL_SECONDS=15` whenever a closing order is in flight or an ACTIVE trade's profit reaches `FAST_POLL_ARM_PCT=0.35` (pre-arm zone, so fast peaks near the +50% trigger aren't missed). Entry scans and invalidation bar-fetches stay on ~60s/50s cadence. *Native BAG stop orders deliberately deferred to live-hardening: IBKR stops on multi-leg combos trigger off noisy spread quotes and behave unrealistically on paper/delayed data — revisit at GO_LIVE Gate 6 with real-time data.* Watch the Peak→Exit gap in the retros to confirm the giveback shrinks.

14. ~~**Confirm exit fills + capture commissions**~~ — ✅ **2026-07-07**: closes go through a `PENDING_EXIT` state; P&L booked from IBKR's `avgFillPrice` on confirmation, with round-trip commissions from `commissionReport`. Unfilled closing orders reprice after 3 min. New `Commission` audit column; alerts + dashboard show net-after-fees. (Motivated by the IBKR YTD NAV report: account −$423 vs audit +$236.)

15. ~~**Daily dollar loss limit**~~ — ✅ **2026-07-07**: `MAX_DAILY_LOSS=400`. Once the day's realized P&L **net of commissions** breaches −$400, no new entries until tomorrow (open positions still managed; EOD flatten unaffected). Checked on every confirmed exit fill; 🛑 Discord alert on breach. Complements the circuit breaker: that one needs *consecutive* losses, this catches interleaved-win bleed days. GO_LIVE Gate 5 requirement.

17. ~~**Skip low-conviction entries**~~ — ✅ **2026-07-08** as `MIN_CONVICTION_SCORE=2` (user chose ≤1 = skip the whole LOW tier, stronger than the original <0 proposal). Evidence: LOW-tier record 1W/5L, −$147 gross, and tiny positions can't clear the per-contract fee floor. `-99` disables.

18. ~~**Throttle only on losing invalidations**~~ — ✅ **2026-07-08**: only invalidation exits worse than −10% count toward `MAX_INVALIDATIONS_PER_SIGNAL`; profitable ones don't (IWM was stood down after two winners). ALL invalidations still feed the conviction penalty via a separate counter (tape character vs signal quality).

9. ~~**Credit-side structures (regime-matched iron condors)**~~ — ✅ **2026-07-08**: on proven range days (ADX < 22, ≥ 8 VWAP crosses, 11:00–13:30 ET, price mid-range), the bot sells an iron condor around the day's high/low (wings `SPREAD_WIDTH` out) as a 4-leg BAG. Sized by max loss (`(width − credit) × 100 × qty ≤ MAX_POSITION_SIZE`). Exits: resting buy-back at 50% of credit, hard stop −70% (=1.7× credit), range-breach invalidation (2 closes beyond a short strike), trail + EOD flatten shared. Mutually exclusive with debit entries by ADX construction. *Evidence: 5 of the first 6 days were premium-seller days.* Known limitation: restart adoption alerts (not reconstructs) open condors. Watch for IBKR error 201 on first fill (may need spread permissions bump).

19. ~~**Take-profit target via resting limit order (+60%)**~~ — ✅ **2026-07-08** as `TAKE_PROFIT_TARGET_PCT=0.60`: on entry fill, a limit sell rests at entry × 1.60. Fills between heartbeats, sells into strength. Every other exit path cancels it first and handles the cancel/fill race (a TP that fills mid-cancel is booked as the exit); external-close detection also cleans it up so no naked short can be left behind. Evidence: max peak ever +64.6%, winners cluster 48–65%; +50% flat target beat the trail on 5 of 7 historical winners. Calibrate 55 vs 60 after ~2 weeks.
