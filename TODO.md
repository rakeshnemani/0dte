# 0DTE Bot — Improvement Backlog

Items to validate or implement once the core strategy has enough paper-trade history.

See [docs/RETROSPECTIVE.md](docs/RETROSPECTIVE.md) for the daily trade journal and
evidence-backed hypotheses behind these items.

---

## Strategy Improvements

1. ~~**ADX slope check (rising vs. flat)**~~ — ✅ **Implemented 2026-07-05** as `ADX_SLOPE_BARS=10` (entry requires ADX rising over the last 10 bars; fails open early-session).
   *Evidence 2026-07-01: ADX direction (entry→exit) predicted all 5 trade outcomes — rising on both winners, collapsing on all three hard-stop losers. See [RETROSPECTIVE.md](docs/RETROSPECTIVE.md).*

2. **Total-exposure cap** — The 3 symbols are one correlated equity-beta bet (07-01: three same-direction positions, three simultaneous hard stops). Cap concurrent positions (e.g. max 2) or same-direction dollar exposure before going live.

3. **[Live transition] Replace SPY with XSP** — Same index, but 60/40 Section-1256 tax treatment + cash settlement (no assignment risk on orphans/expiry) + European exercise. Cost: much wider bid-ask than SPY (~$0.05–0.15 vs $0.01–0.02), so validate fills with small size first. Generate signals from SPY bars (real volume → real VWAP), execute on XSP options. Do **not** trade both — 100% signal duplication.

4. **[Experiment] GLD/TLT on their expiry days** — Only genuine diversification available; no daily 0DTE, so verify their expiration calendar + 0DTE-day option liquidity in IBKR first, then add strike/width configs.

## Pro-Playbook Gaps (from 2026-07-05 industry-standards review)

5. **Time stop (next batch)** — Debit spreads bleed theta; if a trade isn't at ~+15% within 45–60 min, it's failing even if price is flat. All three 2026-07-01 losers were held 90–115 min. Exit-side change, safe to add.
6. **Midday tightening (next batch)** — Require ADX > 30 (vs 25) for entries between 11:30–13:30 ET, the known 0DTE dead zone. All three 07-01 losers entered midday at ADX ~25.5; both winners were open-drive entries.
7. **Expected-move anchor** — Price the ATM straddle at ~10:00 ET to get the day's expected move (helpers already exist). Skip breakout entries once the day has moved >~80% of it (exhaustion filter); later, use for strike/target selection.
8. **VIX1D regime filter** — Fetch `Index('VIX1D', 'CBOE')`; block or downsize entries when 1-day vol is spiking against the trade direction.
9. **[Planned — next major build] Credit-side structures (regime-matched)** — Sell premium (credit spreads / iron condors) on chop days; keep the debit playbook for trend days. *2026-07-06 was the motivating example: SPY pinned in a 5-point range all day — a premium seller's day; our debit strategy lost −$131 on it. User confirmed 2026-07-06: implement soon, after the current chop guards have a stable track record.* Scope: short-spread margin handling, credit-side exit rules (e.g. buy back at 50% of credit received, stop at 2× credit), and a chop-day detector to pick the structure (invalidation-count or ADX-level based).

10. **Invalidation-aware entry throttle (next batch)** — After 2 thesis-invalidation exits on the same (symbol, direction) in one day, stand down on that signal until tomorrow (the signal has been proven chop). 2026-07-06: four SPY CALL re-entries, all invalidated; a throttle at 2 would have cut −$131 to ~−$89 and stops death-by-moderate-cuts on grind days.

---
