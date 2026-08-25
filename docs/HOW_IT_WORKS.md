# How the 0DTE Trading Bot Works

The full lifecycle of the bot — startup, market-hours management, entry scanning, order
execution, position monitoring, exits, and end-of-day flatten. The bot is **single-leg only**:
it buys ONE ATM (~50Δ) option (CALL bullish / PUT bearish) for whichever strategy fires. Two
mechanical strategies run together (`STRATEGY=trend,gex`), each holding its own SPX position keyed
`strategy:symbol`. For the entry/exit rules per strategy see [PLAYBOOKS.md](PLAYBOOKS.md).

> **Not covered in depth here (see the linked docs):** the bot also runs a **`thesis:SPX` human-thesis
> rail** — it watches `data/commands/*.json` (`arm`/`close`/`close_if`/`cancel`) every loop and executes an
> approved thesis via the same single-leg path ([THESIS_GEX.md](THESIS_GEX.md), `data/commands/README.md`).
> GEX/thesis exits are trailing (**arm +35%** peak, give back 20%) + a −80% catastrophe backstop. `audit.csv`
> gained `Max_Adverse_Pct` (MAE) and a log-only `Range_Exp_Ratio` (exhaustion). Thesis-forming tooling:
> `scripts/gex_snapshot.py` + `scripts/gex_dashboard.py` (real-time visual board, served to the phone via
> Tailscale). **CLAUDE.md's "📌 Current state & handoff" block is the freshest summary.**

---

## 1. Startup

On launch the bot (`TradingBot.__init__` → `run()`):
1. Connects to IB Gateway (`127.0.0.1:4002`, clientId 1) and sets market-data type 1 (live).
2. Wires a **data-farm drop/restore Discord alert** (`broker.on_farm_change`) so a feed outage
   pings instead of hiding in the console.
3. Subscribes to account positions (`reqPositions`).
4. **Checks for untracked positions** (`adopt_orphan_positions`): a restart wipes `active_trades`,
   and because the bot is single-leg it can't know which strategy a lone leg belonged to — so it
   does **not** auto-adopt (a mis-routed exit is worse than none). Any option position the account
   still holds is surfaced with a ⚠️ Discord alert for manual handling; the daily 3:55 flatten is
   the backstop. **Don't restart with a position open.**

## 2. The heartbeat loop

The main loop runs on a **~60-second** cadence: reset daily counters if the date rolled, evaluate
exits for every open trade, run the entry scans, then sleep.

**Fast exit watch:** the cadence drops to **15 seconds** (`FAST_POLL_SECONDS`) whenever an exit
needs tight monitoring — a closing order awaiting its fill, or an ACTIVE trade whose profit has
reached `FAST_POLL_ARM_PCT` (35%, approaching a trail trigger). Entry scanning stays on a ~60s
cadence regardless. Fixes the sampling slippage where fast moves blew past exit thresholds between
60-second checks.

## 3. Smart sleep (market closed)

Outside 09:30–16:00 ET the bot sleeps until the next open (waking hourly to keep the IBKR
connection alive). After the close it sends the day summary and regenerates `dashboard.xlsx` once.

## 4. Daily reset

On the first loop of a new ET date, the bot clears the daily trade count, cooldowns, consecutive-loss
counter, circuit breaker, daily-loss flag, and the day's closed-trades list.

## 5. Entry scanning

Both strategies scan every ~60s (they hold independent positions). Each strategy skips a symbol it
already holds. The shared account guards run in `execute_trade` before any order:

- **Circuit breaker** — no new entries after `MAX_CONSECUTIVE_LOSSES` (5) consecutive losses.
- **Daily loss limit** — no new entries once realized net P&L ≤ −`MAX_DAILY_LOSS`.
- **Daily trade cap** — `MAX_TRADES_PER_DAY` (12).
- **Signal cooldown** — a (strategy, symbol, direction) cools down `SIGNAL_COOLDOWN_MINUTES` (30)
  after a fill.
- **Anti-cascade** — never open on top of an untracked option position in the symbol (alert once).

### Trend signal
Inside a `TREND_WINDOWS` slot: Supertrend(7,3) **flips** into a direction, PSAR agrees, Kaufman
chop ≤ `TREND_KAUF_MAX` (50), and entry-time realized vol ≥ `TREND_SKIP_LOWIV`. A flip that forms
but fails the chop/vol gate is logged and fires a throttled **⏸️ "setup skipped" Discord alert** (for
process transparency).

### GEX signal
Inside a `GEX_WINDOWS` slot, with Gflip + concentration walls computed **live** from the IBKR chain
(refreshed every `GEX_REFRESH_MIN`, saved to `data/gex/` for future backtesting): negative-gamma
regime (spot < Gflip) or a wall breakout, **plus** a 15-min opening-range breakout, **plus**
short-term momentum (`GEX_MOMENTUM_BARS`), **plus** the vol gate. A setup that forms but fails a
condition fires the same throttled skip alert. The live regime is logged every ~5 min.

## 6. Order execution (single-leg)

When a signal passes, `execute_trade` computes the ATM strike (nearest `STRIKE_STEP`) and calls
`_place_single_leg`:
- Quotes the option; skips if the mid is below `MIN_OPTION_COST` (a fee floor).
- **Limit price** = mid + `ENTRY_AGGRESSION`·(ask − mid), then **snapped to the valid option tick**
  (`broker.snap_to_tick`). SPX options tick $0.05 below $3.00 premium, **$0.10 at/above** — an
  off-tick limit is rejected with **IBKR error 110** (this once blocked every order; see the bug
  history in CLAUDE.md).
- Submits a `BUY` for **1 contract**, tracks it `PENDING_ENTRY` under `strategy:symbol`, and sends
  the ⏳ submit alert.
- **Entry timeout:** an unfilled entry is cancelled after `ENTRY_ORDER_TIMEOUT_SECONDS` (120s) and
  the signal re-evaluated fresh — a resting limit only fills once the market moved against the
  thesis (#34). The cooldown and daily count are set on the **fill**, not submission, so a timed-out
  entry doesn't lock the signal out.

## 7. Monitoring active trades

Each loop, for every open trade (`evaluate_exit_conditions_for_symbol`):

**Position reconciliation (runs first).** Reconciles the trade against the real IBKR account
(`ib.positions()`), checking the trade's leg conId. Hardened against the 2026-07-09 orphaning bug:
**fail-open on an empty positions feed**, a 90s grace period after a fill, and a **time-based**
(180s) confirmation window before concluding a position was closed externally — so 15s fast-polling
can never drop a live trade. A confirmed external close is dropped from tracking with a ⚠️ alert.

**Pending entry.** If still `PENDING_ENTRY`, check for the fill (→ activate) or the entry timeout.

**Exit rules** route by `trade['strategy']`:
- **Trend:** −`HARD_STOP_LOSS_PCT` (50%) hard stop, Supertrend reversal, EOD flatten. No TP.
- **GEX (let the convex tail ride):** a **trailing stop** that arms once the trade peaks at
  `GEX_TRAIL_TRIGGER` (+35%) then exits on giving back `GEX_TRAIL_GIVEBACK` (20%) of the peak; a
  **wide −`GEX_CATASTROPHE_STOP` (80%) backstop** for a trade that never peaks; EOD flatten. No
  invalidation, no fixed max-loss stop, no TP.

The live option value, current profit %, and running peak (`max_profit_pct`) are cached each loop
for the day summary.

## 8. Closing a position (fill-confirmed)

On an exit trigger, `close_position` submits a `SELL` for the position's option contract at the
current mid **snapped to the valid tick** (the same error-110 rule applies to the close), marks the
trade `PENDING_EXIT`, and confirms the fill before booking:

| Situation | Action |
|---|---|
| Filled | Book P&L from the **actual fill price** + IBKR-reported commissions |
| Still pending after ~3 min | Reprice: amend the limit to the current (tick-snapped) value |
| Rejected / cancelled | Retry, capped at **4 attempts** with a 30s cooldown; then a "close manually" alert |

P&L is never booked at the submission price — only from the confirmed fill. Over-close and
double-fill guards (learnings #26/#30) protect the close path.

## 9. End-of-day flatten & day summary

At `EOD_FLATTEN_TIME` (15:55 ET; GEX also honors `GEX_FLATTEN_TIME`) the bot closes all open
positions — never hold a 0DTE long into the 4 PM close. After the close it posts the day summary
and rebuilds `dashboard.xlsx`.

## 10. Guards & circuit breaker

- **Circuit breaker** trips after `MAX_CONSECUTIVE_LOSSES` consecutive losing trades → no new
  entries for the day (⚠️ alert).
- **Daily loss limit** — realized net P&L ≤ −`MAX_DAILY_LOSS` → no new entries (open trades still
  managed).
- **Data-farm alert** — on IBKR feed drop (error 2103/2105) and restore (2104/2106), an on-change,
  deduped Discord alert fires; a dropped feed blinds the bot (no bars → no entries) until it
  reconnects.

## 11. Configuration reference

All from `.env` (see `src/config.py` for defaults). Key knobs:

| Setting | Default | Meaning |
|---|---|---|
| `STRATEGY` | `trend,gex` | Which strategies run (comma-separated) |
| `SYMBOLS` | `SPX` | Underlyings (SPX = cash-settled, no assignment) |
| `TREND_WINDOWS` | `09:30-14:00` | ET slots trend may enter in |
| `TREND_KAUF_MAX` | `50` | Max Kaufman chop for a trend entry |
| `TREND_SKIP_LOWIV` / `GEX_SKIP_LOWIV` | `0.082` | Skip entries below this entry-time realized vol |
| `GEX_WINDOWS` | `09:30-15:55` | ET slots GEX may enter in |
| `GEX_TRAIL_TRIGGER` / `GEX_TRAIL_GIVEBACK` | `0.35` / `0.20` | GEX trailing stop (arm / giveback) |
| `GEX_CATASTROPHE_STOP` | `0.80` | GEX wide downside backstop |
| `HARD_STOP_LOSS_PCT` | `0.50` | Trend hard stop |
| `MIN_OPTION_COST` | `0.30` | Skip if the option mid is below this (fee floor) |
| `ENTRY_AGGRESSION` | `0.5` | Entry limit mid→ask fraction |
| `ENTRY_ORDER_TIMEOUT_SECONDS` | `120` | Cancel an unfilled entry after this |
| `MAX_TRADES_PER_DAY` / `SIGNAL_COOLDOWN_MINUTES` | `12` / `30` | Trade cap / per-signal cooldown |
| `MAX_CONSECUTIVE_LOSSES` / `MAX_DAILY_LOSS` | `5` / `800` | Circuit breaker / daily loss limit |
| `EOD_FLATTEN_TIME` | `15:55` | Force-close all positions at this ET time |

## 12. Record-keeping

- **`audit.csv`** — one row per fill (permId joins each row to IBKR); reconcile via
  `scripts/reconcile_ibkr.py` **after settlement**.
- **`logs/bot.log`** — operational log (daily rotation, ET).
- **`dashboard.xlsx`** — regenerated after each trading day.
