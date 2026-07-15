# 0DTE Bot — Improvement Backlog

Items to validate or implement once the core strategy has enough paper-trade history.

See [docs/RETROSPECTIVE.md](docs/RETROSPECTIVE.md) for the daily trade journal and
evidence-backed hypotheses behind these items, and [docs/GO_LIVE.md](docs/GO_LIVE.md)
for how they map to the paper→live gates.

## Priority queue

| Priority | Meaning | Items |
|----------|---------|-------|
| **P0 — CRITICAL** | Live-money correctness bugs | ✅ **all clear** — #21/#25/#26 (07-09), #30 close-integrity + #31 path guard (07-10) all done |
| **P0 — do next** | Directly moves the fee-adjusted edge or is a mandatory safety control; small builds | **#3** migrate to XSP (assignment incident 07-13) · **#32** regime-aware exit (VWAP invalidation whipsaws trend days) |
| **P1 — soon** | Required before live or user-committed next major build | **#2** exposure cap, **#16** always-on host, **#22/#28** condor tuning *(#23 hourly summary, #24 reconcile, #9 condors, #17/#18/#19 → done)* |
| **P2 — evidence-gated** | Good hypotheses waiting for data or a trigger day | **#20** wider spreads (fee-ratio experiment), **#5** time stop, **#6** midday tightening, **#7** expected-move anchor, **#12** 2-hour throttle |
| **P3 — parked / live-transition** | By design not now | **#4** GLD/TLT, **#8** VIX1D |

> **⏸️ BOT PAUSED (2026-07-13):** entries stopped after Friday 07-10 assignment
> (400 QQQ + 600 SPY shares, ≈ −$9k) traced to assignable ETF options. Not resuming
> until **#3** (XSP-only, cash-settled) lands. User to flatten the assigned shares.

Rationale for the P0s: after fee adjustment the edge is currently negative (see
GO_LIVE Gate 2). #13 attacks the biggest recoverable leak — winners giving back
10–16 pts to 60-second exit sampling — and doubles as the Gate-4 restart-safety
requirement (native stops). #15 is ~20 lines and caps the day a guard fails.

---

## P0 — Do next

34. **[P0 — NEW 2026-07-15] Entry-order timeout — we get filled on dead signals.** 07-15: a limit BUY submitted at **12:04:42** rested unfilled for **1h 42m** (98 polls) and filled at **13:47:15**, then invalidated **65 seconds later**. A resting limit only fills *when the spread decays to our bid* — i.e. when the market has already moved against the thesis (**adverse selection**). The signal's shelf life is bars, not hours. **Fix: cancel an unfilled entry order after ~2–3 min** (config `ENTRY_ORDER_TIMEOUT_SECONDS`), and re-evaluate rather than resubmit. Also fixes the audit lie: the BUY row stamps the fill time but carries the *signal-time* indicators (103 min stale). Cheap + high value — this alone converts a guaranteed loser into a no-trade.
   - ✅ **DONE 2026-07-15.** `ENTRY_ORDER_TIMEOUT_SECONDS=120`; `bot._expire_stale_entry()` cancels a stale entry limit and drops it (no position). Fill path extracted to `_activate_entry()` and reused, so a **partial fill is promoted with the real qty, never orphaned** (#21/#30) — including the cancel-races-a-fill case, which the old `Cancelled → pop` branch would have dropped (latent pre-existing bug, now fixed too). Discord alert `notify_entry_expired`. Tests: `scripts/test_entry_timeout.py` (15 checks incl. partial-fill rescue + `timeout=0` = old behavior). Note: the 30-min signal cooldown still applies after an expiry, so it won't churn-resubmit.

35. **[P0 — NEW 2026-07-15] Raise `MIN_SPREAD_COST` / cap contracts — cheap spreads are a fee bomb.** 07-15: a **$0.14** spread at a $300 budget ⇒ **21 contracts ⇒ 42 legs ⇒ $95.43 fees** on an $80 gross loss (**fees > the loss**). Worse, the *win* case: +60% TP ⇒ ~$176 gross − ~$95 fees ≈ **$81 net** — risking a certain $95 fee bill to maybe make $81. A cheap spread doesn't cut risk, it **maximizes contract count and therefore fees**. `MIN_SPREAD_COST=0.10` is far too low → try **0.30–0.40**, and/or cap contracts per trade. This is the concrete, actionable face of #20.
   - ✅ **PARTLY DONE 2026-07-15: `MIN_SPREAD_COST` 0.10 → 0.30** (`.env` + config default, with the fee rationale in-comment). At $0.30 a $300 budget buys ~10 lots instead of 21 — roughly **halves the fee bill per trade**. **Still open:** an explicit contracts-per-trade cap, and the wider-spread ($2-wide) experiment (#20) — raising the floor limits the damage but doesn't fix the structural fee ratio.

36. **[P0 — NEW 2026-07-15] Circuit breaker can't fire — it resets daily.** `MAX_CONSECUTIVE_LOSSES=5` but `consecutive_losses` is zeroed in `check_and_reset_daily_trade_count` every day. At 1–2 trades/day it can never reach 5, so **8 consecutive losses across 6 days (−$544) tripped nothing**. It only sees intraday streaks and is blind to the slow bleed we're actually in. **Fix: persist the streak across days, or add a rolling last-N-trades / N-day drawdown guard.**

3. **[P0 — active] Migrate SPY→XSP; run XSP-only (cash-settled, no assignment).** Trigger: **2026-07-10 assignment** — short legs on American-style ETF options (SPY/QQQ condor residuals, incl. the #30 leftover) expired ITM → **400 QQQ + 600 SPY shares assigned over the weekend, ≈ −$9k**. XSP is cash-settled + European → **cannot be assigned**. Decision (07-13): **XSP only** — drop QQQ and IWM (both American-style/assignable; no clean cash-settled mini validated yet). Approach (already specced): **generate signals from SPY 1-min bars** (real volume → real VWAP/ORB/ADX), **execute on XSP options**. Scaffolding exists: `broker.py:96` already builds `Index('SPX','CBOE')`; `option_symbol()` maps roots. Build:
   - `option_symbol`/contract path for XSP (root `XSP`, exchange `CBOE`, European, multiplier 100); Index underlying for level, SPY bars for indicators.
   - Per-symbol **strike increment** (XSP $1) + strike selection from SPY-derived levels (XSP ≈ SPY level).
   - Drop the assignment-prone assumptions; no early-exercise path needed.
   - **Needs live-Gateway validation** (can't test tonight): XSP option-chain qualification, IBKR paper **market-data subscription** for XSP, and **fill quality** at 0DTE (bid-ask ~$0.05–0.15 vs SPY $0.01–0.02 → validate small size first). Code + unit tests can land now; Gateway checks handed over.
   - Do **not** trade SPY and XSP both (100% signal duplication).
   - **STATUS 2026-07-13 — code complete + unit-tested (additive; SPY/QQQ/IWM paths untouched).** `broker.INDEX_SPECS` + `option_exchange()`; `underlying_contract`/`option_symbol`/`get_option_contract`/`make_bag_multi`/`fetch_intraday_data` are index-aware; `config.SIGNAL_SOURCE={'XSP':'SPY'}`; `bot.py` sources entry **and** exit-invalidation bars from SPY for XSP. Tests: `scripts/test_xsp_and_regime.py` (13 mapping checks pass). `.env` `SYMBOLS=XSP`. **⏸ Do NOT restart until the Gateway checklist passes:** (1) `qualifyContracts` an XSP 0DTE option (if CBOE fails, try SMART — flip `INDEX_SPECS['XSP']['option_exchange']`); (2) confirm XSP market-data subscription in paper; (3) 1-lot fill test to check slippage.

32. **[P0 — active] Regime-aware exit — stop whipsawing out of trend days.** 2026-07-13 (a −2% down day) we had PUT direction **right** on SPY/IWM but the **VWAP-recross invalidation** ejected us on a normal pullback, then the downtrend paid off without us (SPY exited 751.45 → closed ~748.2; the 751/750 put would have hit +60% TP). The exit assumes "VWAP recross = thesis dead," which is **false on a trend day with pullbacks** — exactly our best regime. Options (test via `replay_invalidation.py` before committing): (a) **suppress invalidation while ADX is high/rising**, only fire when the trend is actually rolling over; (b) invalidate on a **structural level** (close back beyond the ORB line we broke) instead of a bare VWAP tick; (c) require price to close beyond VWAP by a **buffer**, not touch it. Companion: the **entry** chop-guard (rising-ADX gate) blocked *every* QQQ entry on QQQ's −2% day — same over-fit-to-chop problem on the entry side; re-evaluate the "ADX must be rising" gate for elevated-but-flat ADX trend days. **Root diagnosis: the risk/filter layer is over-fit to chop and strangles trend days — recalibrate, don't rewrite.**
   - **STATUS 2026-07-13 — mechanisms built + unit-tested, all default-OFF (identical to pre-#32 until calibrated).** `strategy.thesis_invalidated` gained (a) `VWAP_INVALIDATION_BUFFER_PCT` (recross must clear VWAP by a margin) and (b) `VWAP_INVALIDATION_HOLD_ADX` (suppress invalidation while ADX ≥ x); `entry_signal` gained `ADX_SLOPE_OVERRIDE_ADX` (enter on flat slope when ADX ≥ x). Tests: `scripts/test_xsp_and_regime.py` (7 checks incl. buf=0/hold=0 = old rule).
   - **CALIBRATED 2026-07-13 (evening)** via the rebuilt `replay_invalidation.py` (now calls the REAL `thesis_invalidated`; the old script used cumulative VWAP, so the prior "N=6 −115bp" was on the wrong line). 33 entries: **N=6 current −60bp (invalidates 32/33!)** · **+buf 0.05% −52bp** (best; 12 whipsaws → 10 EOD + 2 TP, no new hard stops) · +buf 0.10% −254bp (too loose, 4 hard stops) · +hold ADX35/40 ≈ no effect. **Recommend `VWAP_INVALIDATION_BUFFER_PCT=0.0005`; leave hold + override at 0.** Bigger finding: 32/33 invalidations ⇒ the exit isn't the root cause — see #33.
33. **[P1 — new, 2026-07-13] Anchored/session VWAP instead of `ta` rolling-14.** The replay showed the current rolling-14 VWAP *hugs price*, so both the entry "beyond VWAP" breakout and the invalidation "VWAP recross" are near-meaningless (32/33 entries invalidate within bars). A standard **anchored session VWAP** (from the 09:30 open) is a stable line — "beyond VWAP" and "recross" would carry information. Touches `add_indicators` (entry) and `thesis_invalidated` (exit) together; re-run the replay after. Likely the real edge lever — entries are chronically wrong-side-of-VWAP right after entry, which is an entry-quality/reference problem, not exit patience.

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

*(#3 XSP promoted to P0 — active — after the 2026-07-10 assignment. See above.)*

4. **[P3 — experiment] GLD/TLT on their expiry days** — Only genuine diversification available; no daily 0DTE, so verify their expiration calendar + 0DTE-day option liquidity in IBKR first, then add strike/width configs.

8. **[P3] VIX1D regime filter** — Fetch `Index('VIX1D', 'CBOE')`; block or downsize entries when 1-day vol is spiking against the trade direction. Cheap fetch, but another threshold to calibrate — revisit after the conviction score's calibration pass proves the current regime signals out.

---

## ✅ Done

21. ~~**Reconciliation false-positives orphan live positions**~~ — ✅ **2026-07-09**. `_position_still_open()` rewritten: **fails open on an empty positions feed** (an account with an open 0DTE spread always shows ≥2 legs, so empty = feed-not-ready, not closed — the actual bug), checks whether **any** leg is held (all leg conIds now stored per trade, incl. all 4 condor legs), and the drop decision is **time-based (180s of consistent absence)** not loop-count so 15s fast-poll can't drop a live trade in 30s. Added an **anti-cascade entry guard**: `execute_trade`/`execute_condor` refuse to open while the account holds untracked legs for that symbol (⚠️ alert once/day), so a phantom-close can't spawn a duplicate. Unit-tested (empty-feed → still-open, any-leg → still-open, live-feed-absent → closed, entry guard). Root-causes #26 too.

26. ~~**Order reject/retry infinite loop on error 201**~~ — ✅ **2026-07-09**. `close_position()` now caps retries: attempts are spaced by a 30s cooldown, and after 4 rejections the bot sets `close_failed`, fires a 🛑 **CLOSE FAILED — MANUAL ACTION NEEDED** alert, and **stops auto-retrying** (keeps the trade tracked — never orphans it). On an error-201 rejection specifically, it sweeps stray open orders on the underlying (`cancel_open_orders_for`) before the next attempt, and logs the exact IBKR error code/message from the order log. Root cause was already removed by #21 (no orphan cascade → no conflicting orders); this bounds the loop as belt-and-suspenders. Unit-tested (4 attempts → give up + alert).

25. ~~**permId as a tracked key + in audit**~~ — ✅ **2026-07-09**. `broker.order_perm_id()` reads IBKR's permanent order id; captured as `entry_permId`/`exit_permId` on the trade and written to a new `PermId` audit column (both BUY and SELL rows). `scripts/backfill_permid.py` retro-filled today's rows by matching IBKR executions on (symbol, price, time) — 9/10 matched (older rows are outside IBKR's ~24h execution window; need Flex Query per #24). permId is the exact join key for reconciliation and audit↔IBKR.

24. ~~**Ad-hoc P&L reconciliation by date**~~ — ✅ **2026-07-09**. `scripts/reconcile_ibkr.py [date] [--write]` now: (a) auto-uses an **IBKR Flex Query** for dates older than the ~24h live-API window (`IBKR_FLEX_TOKEN`/`IBKR_FLEX_QUERY_ID` env vars — code done, needs the user's one-time Flex setup); (b) **joins on `permId`** for exact per-order matching, flagging IBKR orders with no audit row as ORPHANS, plus an audit-internal "BUY with no SELL" check; (c) **`--write`** appends orphan RECONCILE rows to the audit (backs up first). permId-join + orphan detection unit-tested offline against the real 07-09 audit (caught the unbooked order and the SPY/IWM orphans).

30. ~~**Close orders can double-fill → untracked residual position**~~ — ✅ **2026-07-10**. The close path no longer trusts order *status*; it trusts **fills and the account position**: (1) a `Cancelled`/`Inactive` closing order has its **fills checked first** — fully filled → booked (the exact 07-10 bug), partially filled → the slice is booked (audit + day record) and the tracked qty shrinks to the true remainder; (2) **every close submission requantifies against the account** via the always-long reference leg — remaining 0 with prior fills → book, 0 without → drop as external (no fabricated P&L), **negative → OVER-CLOSED halt + 🚨 inverse-position alert**, unknown feed → defer (never blind-submit); (3) `last_order_error` now **skips informational codes** (the `10349` red herring) and returns the real rejection; (4) retries **sweep stray open orders** on the legs first (the 201 fix). Unit-tested across all 7 scenarios: `scripts/test_close_integrity.py`.

31. ~~**Path-aware entries (the bear-trap fix)**~~ — ✅ **2026-07-10** as `strategy.path_confirms()` (pure, unit-testable), hard-gated in `entry_signal`: (1) **freshness** — the trigger level must have been crossed within `PATH_FRESH_BARS=10` (at least one recent close on the other side; blocks stale-break hovering); (2) **micro-momentum** — net move of the last `PATH_MOMENTUM_BARS=3` closes must agree with the signal (never fade the last 3 bars). Verified by trace: blocks both 5/5 bear traps (07-09 QQQ +0.96 net-rising against PUT; stale hovers) while passing genuine fresh breaks. Passing detail appended to the signal reason for audit visibility. *User decision: keep `CONVICTION_HIGH_MULT=1.5` — this fix repairs the entries the multiplier amplifies, rather than shrinking the multiplier.*

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
