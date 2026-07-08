# Go-Live Readiness Checklist

When can this bot switch from paper to real money? When **every gate below passes** —
not when a good week feels convincing. Update the status boxes as evidence accumulates
(the retro + dashboard supply most numbers). Last updated: **2026-07-07**.

## Progress

```
Gate 1  Sample size        ██░░░░░░░░  ~18%   (18 of 100 fixed-era trades)
Gate 2  Performance        ░░░░░░░░░░  blocked (fee-adjusted P&L negative — see below)
Gate 3  Regime coverage    ████░░░░░░  ~40%   (3 of 5 regimes seen)
Gate 4  System reliability ████░░░░░░  ~40%   (features done; ops environment not)
Gate 5  Risk controls      ███░░░░░░░  ~30%   (exits proven; caps missing)
Gate 6  Live mechanics     ░░░░░░░░░░  0%     (not started — by design)
Gate 7  Process/runbook    ░░░░░░░░░░  0%     (not started)
─────────────────────────────────────────────
Overall                    ██░░░░░░░░  ~20%
```

**Estimated timeline if all goes well: 6–8 more weeks of paper trading.**

---

## Gate 1 — Sample size

The current exit/sizing stack went live 2026-06-30 (exits) → 2026-07-06 (sizing).
Everything before that is a different strategy.

- [ ] **≥ 100 closed trades** under the current rule set *(now: ~18)*
- [ ] **≥ 30 trading days** of continuous operation *(now: 5)*
- [ ] **No strategy changes in the final 2 weeks** — the last stretch must test a
      frozen system, not a moving target

## Gate 2 — Performance (fee-adjusted) ⚠️ THE REAL BAR

Paper P&L ignores commissions. Real math: IBKR charges ~$0.65/contract/leg, and a
vertical spread is 2 legs in + 2 legs out = **~$2.60 per contract round trip**.
A typical 5-contract trade costs **~$13 in fees**; a 4-trade day ~$50.

**Confirmed by the actual IBKR paper account (YTD NAV report, 2026-07-07):**
Fees & Commissions **−$275.28 actual**, MTM **−$148** → net trading YTD **≈ −$423**
(the account's +$3.4k NAV gain is entirely interest on idle paper cash, not trading).
The account also disagrees with our audit (+$236) by ~$660 — explained by May
experiments, orphaned/manually-closed positions the audit never captured, and the
fact that **audit exit prices are limit-submission prices, never confirmed fills**
(the entry side confirms `avgFillPrice`; the exit side assumes — see TODO #14).
At current trade frequency and size, the edge does not yet clear the friction.
Paths that fix this: fewer/higher-conviction trades, larger size per trade
(fees scale linearly but conviction should concentrate them in winners), wider
spreads (more premium per contract), or better win capture (TODO #13 slippage fix).

- [ ] **Fee-adjusted cumulative P&L > 0** over the full Gate-1 sample
      (model: $0.65 × contracts × 4 per round trip; add ~$0.02/spread slippage haircut)
- [ ] **Profit factor ≥ 1.3 after fees** (dashboard KPI, manually fee-adjusted)
- [ ] **Win rate ≥ 45%** sustained (breakeven is ~38–40% with current exits)
- [ ] **3 of any 4 consecutive weeks profitable** after fees
- [ ] **Max daily loss ≤ $350** across the whole sample (worst so far: −$305)
- [ ] **Conviction calibration confirmed**: HIGH-tier trades outperform LOW-tier
      in the dashboard's "By Conviction Tier" chart (else recalibrate and restart Gate 1's clock)

## Gate 3 — Regime coverage

The bot must survive (not necessarily profit from) every market type:

- [x] **Trend-up day** — 06-30 (+$645), 07-01 morning
- [x] **Flat chop day** — 07-06 (−$131), acceptable damage
- [x] **Bearish chop / PUT day** — 07-07 (+$14), PUT path proven
- [ ] **Full trend-down day** — sustained selloff with PUT winners
- [ ] **Reversal day under current rules** — 07-01 predates sizing/throttle; need a repeat
- [ ] **High-volatility event day** (FOMC, CPI, jobs report) — survive without
      catastrophic loss; ideally the guards keep the bot small or out
- [ ] **Chop-day average ≥ −$75** across all chop days in the sample

## Gate 4 — System reliability

- [x] Startup position adoption (restart-safe)
- [x] External-close reconciliation
- [x] EOD flatten + day summary + dashboard automation
- [x] Modular architecture (broker/strategy/notifier separated)
- [ ] **Always-on host** — bot runs on a VPS or dedicated always-on machine, not a
      laptop that sleeps (Ctrl+C/lid-close incidents: 2 so far)
- [ ] **20 consecutive sessions with zero manual interventions** and zero
      unmanaged positions
- [ ] **Restart drill passed**: kill the bot mid-position, restart, verify adoption
      manages it to a correct exit
- [x] **Exit slippage fix (TODO #13)** — fast polling implemented 2026-07-07
      (60s → 15s while exits need watching, pre-armed at +35% profit). Native
      BAG stop orders deferred to Gate 6 (combo stops trigger off noisy quotes;
      revisit with real-time data). Verify the Peak→Exit gap shrinks in retros
- [ ] **Disconnect drill**: kill IB Gateway mid-session; verify reconnect + state intact

## Gate 5 — Risk controls

- [x] Hard stop, invalidation exit, trailing stop — all observed working live
- [x] Signal throttle + conviction down-sizing — observed working (07-07)
- [ ] **Circuit breaker observed firing** (5 consecutive losses — never happened yet;
      if it never fires in 30 days, that's fine, but verify it in a forced test)
- [x] **Daily dollar loss limit** — implemented 2026-07-07: `MAX_DAILY_LOSS=400`,
      checked net-of-commissions on every confirmed exit fill; 🛑 alert on breach
- [ ] **Total exposure cap (TODO #2)**: max concurrent same-direction dollars —
      *not yet implemented; required before live*
- [ ] **Account sizing plan written**: risk per trade ≤ 2–3% of live account;
      at $300 budgets that implies a $10–15k account, or smaller budgets

## Gate 6 — Live-transition mechanics (start only after Gates 1–5 pass)

- [ ] **Fill-quality validation**: 2 weeks of 1-contract live trades; compare live
      fills vs paper mids; abort if average slippage > $0.03/spread per side
- [ ] **SPY → XSP decision executed** (TODO #3): tax + cash settlement vs liquidity;
      validated with the 1-contract phase
- [ ] **Real-time market data subscribed** (live shouldn't run on delayed data)
- [ ] **Live ramp plan**: 25% sizing for 2 weeks → 50% for 2 weeks → 100%,
      with a "drop back a level after any −$500 week" rule
- [x] **Fee/commission tracking added** to audit.csv (2026-07-07: `Commission` column
      from IBKR `commissionReport`; exits booked at confirmed `avgFillPrice`;
      day summary + dashboard show net-after-fees)

## Gate 7 — Process & runbook

- [ ] **Runbook written**: what to do on — IB Gateway disconnect, position stuck
      unfilled at EOD, early assignment (if still on SPY), bot crash mid-position,
      fat-finger config error
- [ ] **Kill switch documented**: one command to flatten everything and stop
- [ ] **Weekly review ritual**: retro + dashboard + calibration check each Friday
- [ ] **Monitoring**: alert if the bot goes silent during market hours
      (dead-man's-switch, e.g. heartbeat message every N hours)

---

## The honest bottom line (2026-07-07)

The strategy machinery is largely built and visibly improving — chop-day losses
went −$305 → −$131 → +$14. But three things gate everything else:

1. **Fees flip the current P&L negative.** The edge must grow (bigger wins via
   #13, better selection via conviction calibration) or the cost per trade must
   shrink (fewer, larger trades). This is the #1 number to watch weekly.
2. **Sample is tiny.** 18 trades tells us the machine works, not that it wins.
3. **The ops environment is a laptop.** Live money on a machine that sleeps is
   how positions get orphaned at the worst moment.

Re-run this assessment every Friday; move the bars only on evidence.
