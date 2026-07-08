# 0DTE Bot — Improvement Backlog

Items to validate or implement once the core strategy has enough paper-trade history.

See [docs/RETROSPECTIVE.md](docs/RETROSPECTIVE.md) for the daily trade journal and
evidence-backed hypotheses behind these items, and [docs/GO_LIVE.md](docs/GO_LIVE.md)
for how they map to the paper→live gates.

## Priority queue

| Priority | Meaning | Items |
|----------|---------|-------|
| **P0 — do next** | Directly moves the fee-adjusted edge or is a mandatory safety control; small builds | *(cleared — #13 and #15 done 2026-07-07)* |
| **P1 — soon** | Required before live or user-committed next major build | **#2** exposure cap, **#16** always-on host, **#9** credit spreads |
| **P2 — evidence-gated** | Good hypotheses waiting for data or a trigger day | **#5** time stop, **#6** midday tightening, **#7** expected-move anchor, **#12** 2-hour throttle |
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

9. **[P1 — next major build] Credit-side structures (regime-matched)** — Sell premium (credit spreads / iron condors) on chop days; keep the debit playbook for trend days. *2026-07-06 was the motivating example: SPY pinned in a 5-point range all day — a premium seller's day; our debit strategy lost −$131 on it. User confirmed: implement soon, after the current chop guards have a stable track record.* Scope: short-spread margin handling, credit-side exit rules (e.g. buy back at 50% of credit received, stop at 2× credit), and a chop-day detector to pick the structure — the conviction score's LOW tier is the natural trigger. Start after the P0s land and a week of clean data.

## P2 — Evidence-gated

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

1. ~~**ADX slope check (rising vs. flat)**~~ — ✅ **2026-07-05** as `ADX_SLOPE_BARS=10` (entry requires ADX rising over the last 10 bars; fails open early-session).
   *Evidence 2026-07-01: ADX direction (entry→exit) predicted all 5 trade outcomes.*

10. ~~**Invalidation-aware entry throttle**~~ — ✅ **2026-07-06** as `MAX_INVALIDATIONS_PER_SIGNAL=2`: after 2 thesis-invalidation exits on a (symbol, direction) in one day, the signal stands down until tomorrow. ⛔ Discord alert on trip. First fired 2026-07-07 (SPY PUT); counterfactual cleared it.

11. ~~**Conviction-based position sizing**~~ — ✅ **2026-07-06** (both tiers live per user — paper trading): score 0–5 (+1 each: ADX ≥ 30, slope ≥ +3, cross-symbol agreement, pre-11:00 ET entry, ≤ 4 VWAP crosses; −1 per invalidation today) → LOW 0.5× / MEDIUM 1× / HIGH 1.5× budget. Logged to audit, shown in Discord, charted in dashboard. **Calibration pass pending** (~2 weeks of `Conviction` column data). First live save 2026-07-07: LOW sizing halved a losing re-entry.

13. ~~**Fix exit-fill sampling slippage**~~ — ✅ **2026-07-07** via fast polling: the loop drops 60s → `FAST_POLL_SECONDS=15` whenever a closing order is in flight or an ACTIVE trade's profit reaches `FAST_POLL_ARM_PCT=0.35` (pre-arm zone, so fast peaks near the +50% trigger aren't missed). Entry scans and invalidation bar-fetches stay on ~60s/50s cadence. *Native BAG stop orders deliberately deferred to live-hardening: IBKR stops on multi-leg combos trigger off noisy spread quotes and behave unrealistically on paper/delayed data — revisit at GO_LIVE Gate 6 with real-time data.* Watch the Peak→Exit gap in the retros to confirm the giveback shrinks.

14. ~~**Confirm exit fills + capture commissions**~~ — ✅ **2026-07-07**: closes go through a `PENDING_EXIT` state; P&L booked from IBKR's `avgFillPrice` on confirmation, with round-trip commissions from `commissionReport`. Unfilled closing orders reprice after 3 min. New `Commission` audit column; alerts + dashboard show net-after-fees. (Motivated by the IBKR YTD NAV report: account −$423 vs audit +$236.)

15. ~~**Daily dollar loss limit**~~ — ✅ **2026-07-07**: `MAX_DAILY_LOSS=400`. Once the day's realized P&L **net of commissions** breaches −$400, no new entries until tomorrow (open positions still managed; EOD flatten unaffected). Checked on every confirmed exit fill; 🛑 Discord alert on breach. Complements the circuit breaker: that one needs *consecutive* losses, this catches interleaved-win bleed days. GO_LIVE Gate 5 requirement.
