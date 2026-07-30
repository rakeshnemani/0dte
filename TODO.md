# 0DTE Bot — Improvement Backlog

Companion docs: [docs/RETROSPECTIVE.md](docs/RETROSPECTIVE.md) (daily journal + evidence),
[docs/BACKTESTING.md](docs/BACKTESTING.md) (the analysis tooling + how to reproduce every
number below), [docs/GO_LIVE.md](docs/GO_LIVE.md) (paper→live gates).

## Where we are (2026-07-28)

▶️ **RUNNING on SPX, BASELINE-flipped** (`FLIP_DIRECTION=true`; #41 fixed). After a month of
losses, **fading our own breakout signals is the first positive-*gross* signal we've found**
(#42). Not profitable yet — a paper experiment, watched daily. The whole game now: does the
flip's thin (~+2 bp/trade) gross edge survive real SPX fees? SPX (10× notional/contract, tight
$0.10 fills) is what makes that even plausible.

## Priority queue

| Priority | Items |
|---|---|
| **P0 — live experiment** | **#42** flip — capture real SPX fill costs (the fee-edge gate); watch for trend-day bleed |
| **P1 — soon** | **#36** cross-day circuit breaker · **#38** trail-trigger 0.50→0.45 · **#20/#35** fee ratio (wider spreads / contract cap) · **#2** total-exposure cap · **#16** always-on host |
| **P2 — evidence-gated** | **#33** anchored/session VWAP · **#5** time stop · **#6** midday tightening · **#7** expected-move anchor · **#12** throttle stand-down · **#22/#28** condor tuning (condors OFF) |
| **P3 — parked** | **#4** GLD/TLT · **#8** VIX1D |
| **Resolved by evidence — do not reopen** | **#39** (filters aren't the bottleneck) · **#37** (→ #42) · **regime-router** (tested 07-28, not justified) |

---

## P0 — Live experiment

**42. Fade-the-breakout (flip) — the one live experiment.** `backtest_39.py` (signal-replay,
20 days, proxy, fees excluded): flipping every signal goes POSITIVE — **BASELINE-flipped 61%
win / +50 bp / 23 trades** (as-taken is 39% / −9 bp). Live via `FLIP_DIRECTION=true` (config
flag, `bot._maybe_flip`, unit-tested; keeps all filters, inverts only the executed CALL/PUT).
**Needs a bot restart on the #41-fixed code to activate.** Two open gates:
- **(a) fee knife-edge — MEASURED 07-28: $6.52 round-trip** (1-contract 5-wide SPX; ~2.8% of a
  $230 debit, ~4× cheaper than XSP). In backtest units ≈ **1.9 bp/trade** vs the flip's ~2.2 bp
  gross edge → **net ≈ +0.3 bp/trade = breakeven** before slippage. So the flip is *not dead but
  not a clear winner* — **need a real sample to see if the +2.2 bp gross even holds live.** First
  live flip trade (07-28) was a −$46.52 loss (n=1, the flip's regime risk: the CALL signal was right).
- **(b) trend-day bleed** — flipping = fading breakouts, which theory says dies in a trend. The
  07-28 regime split *tempered* this (flip was +33 bp even on trend-labeled bars), so a stand-
  down guard is **not urgent** — but the sample is tiny; **watch live and flip back to false if
  a trending stretch bleeds it.**
- **(c) NEW watch (07-29): SPX fill rate.** 07-29 = 0 trades: 2 orders submitted, both timed out
  unfilled (#34). Despite tight $0.10/leg SPX spreads, mid-limits may not be filling. If it
  persists we starve for trades — check whether it's the flipped-limit pricing or slow tape.

---

## P1 — Soon

**36. Circuit breaker can't fire — it resets daily.** `MAX_CONSECUTIVE_LOSSES=5` but
`consecutive_losses` is zeroed in `check_and_reset_daily_trade_count` every day. At 1–2
trades/day it never reaches 5, so **8 straight losses across 6 days (−$544) tripped nothing**.
Fix: persist the streak across days, or add a rolling last-N-trades / N-day drawdown guard.

**38. Lower `TAKE_PROFIT_TRAIL_TRIGGER` 0.50 → ~0.40–0.45.** 07-20's winner peaked **+48.57%**
but the trail arms only at +50% — missed by 1.4 pts, so the invalidation booked it at +17% on
the giveback. Winners peak and revert fast. Low-risk (trail never exits at a loss). Validate
with a quick replay of the proxy-winners first.

**20 / 35. Fee ratio — wider spreads / contract cap.** `MIN_SPREAD_COST` already 0.10→0.30
(07-15). Still open: an explicit **contracts-per-trade cap**, and testing **wider spreads**
(more premium/contract → fewer contracts → lower fee ratio). On SPX this is less acute (already
~4× cheaper than XSP) but the flip's edge is thin, so every bp of fee matters. Gated on the
#42 real-fee number.

**2. Total-exposure cap.** Cap concurrent positions / same-direction dollar exposure (07-01:
three same-direction positions, three simultaneous hard stops). Small guard in `execute_trade`,
same shape as the daily-loss limit. GO_LIVE Gate 5.

**16. Always-on host.** Move the bot off the laptop (VPS / dedicated box; interim tmux +
caffeinate). Repeated down-days (07-20 missed the morning; 07-23/24 down). GO_LIVE Gate 4's
"20 clean sessions" clock can't start until this lands. Operational, not code.

---

## P2 — Evidence-gated

**33. Anchored/session VWAP instead of `ta` rolling-14.** The rolling-14 VWAP *hugs price*, so
both the entry "beyond VWAP" and the "recross" invalidation are near-meaningless (they fire on
almost everything). A stable **session-anchored VWAP** would make those signals carry
information. Touches `add_indicators` (entry) + `thesis_invalidated` (exit); re-run the replay
after. *Note: the flip (#42) may make this moot — if we're just fading a noisy line, a better
line might not help. Revisit only if #42 stalls.*

**5. Time stop** — exit if not at ~+15% within 45–60 min (theta bleed). *Invalidation already
cuts most; needs evidence the slow-bleed case still occurs.*
**6. Midday tightening** — require ADX>30 for 11:30–13:30 entries. *Partly covered by conviction `early✗`.*
**7. Expected-move anchor** — price the ATM straddle ~10:00 ET; skip breakouts once the day has
moved >80% of it (exhaustion). Also usable for strike/target selection.
**12. Throttle stand-down 2h instead of all-day** — implement when a real morning-chop→afternoon-trend day shows the cost.
**22 / 28. Condor tuning** — condors are **OFF** (`CONDOR_ENABLED=false`, net drag 1W/2L). If ever
re-enabled: breach exit fires too late (#22, exited −67% not −25%), and the R:R needs an 86% WR
(#28). Also latent: condor strikes are computed from SPY (signal) levels but placed on the
execution symbol — broken for index symbols until fixed. Leave off.

---

## P3 — Parked

**4. GLD/TLT on their expiry days** — only genuine diversification; verify their 0DTE calendar +
liquidity first. **8. VIX1D regime filter** — `Index('VIX1D','CBOE')`; downsize/block when 1-day
vol spikes against the trade. Cheap fetch, another threshold to calibrate.

---

## Resolved by evidence — do not reopen

**39. "The entry filters strangle trend days."** WRONG (07-27 backtest). Un-blocking entries
(override + freshness-off) made it *worse* — 39%→35% win, −9→−134 bp; the added trades are 31%
win, net-negative, and bad even with the invalidation exit off. The guards are **net-helpful**;
the invalidation exit is **net-protective** (−9 with it vs −63 without). No cheap entry-filter
fix exists — the problem is the breakout premise + fees, not the filters. **Keep guards as-is.**

**37. Follow-vs-fade regime detection.** Superseded by #42 — the flip *is* the concrete form of
"we're systematically on the wrong side." The detection question is answered by the regime
split below.

**Regime-router (chop→flip, trend→follow) — tested 07-28, NOT justified.** The premise test
(`backtest_39.py regime_split`, baseline entries by entry-bar regime):
`CHOP: follow 20% / −42bp · flip 60% / +17bp` — **flip wins in chop ✓**;
`TREND: follow 54% / +33bp · flip 62% / +33bp` — **follow does NOT beat flip; a wash ✗**.
So there's no regime where following wins → routing adds a fragile lagging classifier for zero
measured gain, and would switch to *follow* exactly where *flip* was equal-or-better. **Keep
plain BASELINE-flipped (#42); don't build the router.** Small sample (10 chop + 13 trend) and a
crude classifier — if a much bigger live sample later shows a clean "follow wins in strong
trends" pocket, revisit; let *data* trigger that, not intuition.

---

## ✅ Done

**41. SPX combo orders rejected (error 478)** — ✅ **2026-07-28**. `make_bag_multi` set `bag.symbol`
to the option root `'SPXW'` while the legs are on underlying `SPX` → 478 rejected every SPX
order. Fixed: `bag.symbol = symbol`. Unit-tested (`test_xsp_and_regime.py`). Validator hardened
with a `[4b]` step: deterministic `bag.symbol == leg-underlying` check + a `whatIfOrder` that
runs IBKR's real order validation without placing it and prints the est. commission (the #42 fee
number; needs market hours for a populated state).

**40. Switch XSP → SPX (fee lever)** — ✅ **2026-07-22** (intraday-validated). SPX = 10× notional/
contract → ~4× fewer fees; cash-settled European (no assignment); far more liquid than XSP.
No code changes needed (`INDEX_SPECS['SPX']` already correct). Config: `SYMBOLS=SPX`,
`SIGNAL_SOURCE['SPX']='SPY'`, `STRIKE_STEP[SPX]` 25→5, `MAX_POSITION_SIZE` 300→400 (LOW skip /
MED 1 / HIGH 2 spreads), `MAX_DAILY_LOSS` 400→800. Validated: ATM 5-wide debit ~$2.15;
bid/ask $0.10/leg (~0.7%, vs XSP 20–40%). Supersedes #3.

**3. Migrate to XSP (cash-settled, no assignment)** — ✅ **2026-07-13**, validated live 07-14;
**superseded by SPX (#40) 07-22.** Triggered by the 07-10 assignment (~−$9k, 400 QQQ + 600 SPY
shares from assignable ETF options). Built `INDEX_SPECS`, `option_exchange()`, index-aware
contract path, `SIGNAL_SOURCE` (signals from SPY bars, execute on the index).

**34. Entry-order timeout (adverse-selection fills)** — ✅ **2026-07-15**, validated live 07-17.
A limit that sits unfilled only fills once the spread decays to our bid — i.e. once the market
moved against us (07-15: sat 1h42m, filled, invalidated 65s later). `ENTRY_ORDER_TIMEOUT_SECONDS
=120`; `_expire_stale_entry` cancels + drops. Fill path extracted to `_activate_entry` so a
**partial fill is promoted with the real qty, never orphaned** (also fixed a latent Cancelled→pop
orphan bug). Tests: `test_entry_timeout.py` (15). Partial-fill rescue fired live 07-20.

**35. `MIN_SPREAD_COST` 0.10 → 0.30 (fee bomb)** — ✅ **2026-07-15**. A cheap spread buys the most
contracts → the most fees (07-15: $0.14 spread = 21 lots = $95 fees on an $80 loss). Contract
cap still open (see #20/#35 above).

**32. Regime-aware exit — mechanisms built, left OFF (evidence: not the lever).** ✅ built +
unit-tested 2026-07-13: `VWAP_INVALIDATION_BUFFER_PCT`, `VWAP_INVALIDATION_HOLD_ADX` (direction-
aware DI check), `ADX_SLOPE_OVERRIDE_ADX`. Replay recommended buffer=0.0005 as a small win, but
the 07-27 #39 backtest showed the invalidation exit is net-protective and the entry filters
aren't the bottleneck — so all three knobs stay **0/off**. Kept as tuning levers, not enabled.

**21. Reconciliation false-positives orphan live positions** — ✅ **2026-07-09**. `_position_still_open`
fails open on empty feed, checks any-leg, drops on 180s time-based absence (not loop-count);
anti-cascade entry guard. Unit-tested.

**26. Order reject/retry infinite loop (error 201)** — ✅ **2026-07-09**. `close_position` caps at
4 attempts w/ 30s cooldown + give-up alert + error-201 order sweep. Unit-tested.

**30. Close orders double-fill → untracked residual** — ✅ **2026-07-10**. Close path trusts fills +
account position, never status: fills-check on dead orders, per-submission requantification,
over-close halt + inverse-position alert. Unit-tested (`test_close_integrity.py`, 7 scenarios).

**31. Path-aware entries (bear-trap fix)** — ✅ **2026-07-10**. `path_confirms()`: freshness (level
crossed within `PATH_FRESH_BARS=10`) + micro-momentum (last `PATH_MOMENTUM_BARS=3` agree).
*(Note: #39 later showed the freshness half is imperfect but net-helpful — keep it.)*

**25. permId tracking + audit column** — ✅ **2026-07-09**. `order_perm_id()` → `PermId` audit
column (BUY+SELL); `backfill_permid.py`. The join key for reconciliation.

**24. Reconcile P&L by date** — ✅ **2026-07-09**. `reconcile_ibkr.py [date] [--write]`, Flex-Query
path for >24h dates, permId join, orphan detection two ways.

**23. Hourly Discord health summary** — ✅ **2026-07-09**. `notify_hourly_health` once/ET-hour.

**27. Operational logging** — ✅ **2026-07-09**. `logs/bot.log`, daily rotation, ET, 30-day.

**1. ADX slope check** — ✅ **2026-07-05** (`ADX_SLOPE_BARS=10`).
**10. Invalidation-aware throttle** — ✅ **2026-07-06** (`MAX_INVALIDATIONS_PER_SIGNAL=2`).
**11. Conviction sizing** — ✅ **2026-07-06** (0.5×/1×/1.5× by 0–5 score).
**13. Exit-fill sampling fix** — ✅ **2026-07-07** (fast poll 60→15s when arming/in-flight).
**14. Confirm exit fills + commissions** — ✅ **2026-07-07** (`PENDING_EXIT`, `Commission` column).
**15. Daily dollar loss limit** — ✅ **2026-07-07** (`MAX_DAILY_LOSS`).
**17. Skip low-conviction entries** — ✅ **2026-07-08** (`MIN_CONVICTION_SCORE=2`).
**18. Throttle only on losing invalidations** — ✅ **2026-07-08**.
**9. Iron condors (regime-matched credit side)** — ✅ **2026-07-08**; **disabled 07-10** (net drag).
**19. Take-profit resting limit (+60%)** — ✅ **2026-07-08** (`TAKE_PROFIT_TARGET_PCT=0.60`).
**29. Condor setup-quality in alerts** — ✅ **2026-07-09**.
