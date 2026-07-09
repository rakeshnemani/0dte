# 0DTE Paper Trading Bot

A Python algorithmic trading bot that paper trades 0DTE options spreads on Interactive Brokers (IBKR) using VWAP, ADX, and 30-minute Opening Range Breakout signals.

## Project Structure

```text
0dte/
├── .env                 # Environment variables and configuration
├── .gitignore
├── README.md
├── requirements.txt     # Python dependencies
├── audit.csv            # Trade log (auto-created on first run)
├── dashboard.xlsx       # Excel dashboard (regenerated after each trading day)
├── docs/
│   ├── HOW_IT_WORKS.md  # Deep-dive on bot logic and design decisions
│   ├── RETROSPECTIVE.md # Daily trade journal + hypotheses under test
│   └── GO_LIVE.md       # Paper→live readiness gates and progress
├── scripts/
│   ├── build_dashboard.py  # audit.csv → dashboard.xlsx
│   └── counterfactual.py   # "what did SYMBOL do after HH:MM?" retro helper
└── src/
    ├── __init__.py
    ├── main.py          # Entry point (asyncio loop setup + run)
    ├── config.py        # Env/config loader
    ├── bot.py           # TradingBot — state + orchestration + main loop
    ├── broker.py        # IBKRBroker — connection, market data, orders, positions
    ├── strategy.py      # Pure signal logic — indicators, entries, conviction, exits
    ├── notifier.py      # Discord transport + every message template
    ├── audit.py         # audit.csv writer
    └── market_time.py   # ET market-hours helpers
```

---

## Prerequisites

### 1. Install IB Gateway (or TWS)

The bot communicates with IBKR over a local socket — IB Gateway must be running before you start the bot.

1. Download **IB Gateway**: https://www.interactivebrokers.com/en/trading/ibgateway-stable.php
2. Log in with your **Paper Trading** credentials (separate from live — find them in IBKR Account Management)
3. Go to **Configure → Settings → API → Settings**:
   - ✅ Enable ActiveX and Socket Clients
   - ☐ Read-Only API (uncheck this)
   - Socket port: `4002`
   - Add `127.0.0.1` to Trusted IP Addresses
4. Keep IB Gateway running whenever the bot is active

> Using full TWS instead? Set `IBKR_PORT=7497` in `.env`.

### 2. Enable options trading permissions

The paper account needs options permissions to submit spread orders.

1. Log into [IBKR Client Portal](https://www.interactivebrokers.com/)
2. Go to **Settings → Account Settings → Trading Permissions**
3. Enable **US Securities Options — Level 2** (required for debit spreads)
4. Once approved on your live account, the paper account inherits the same permissions automatically

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure `.env`

```env
# IBKR Connection
IBKR_HOST=127.0.0.1
IBKR_PORT=4002              # 4002 = IB Gateway paper, 7497 = TWS paper
IBKR_CLIENT_ID=1

# Symbols
SYMBOLS=SPY,QQQ,IWM

# Position sizing
MAX_POSITION_SIZE=300.0     # Max dollars risked per spread
MAX_TRADES_PER_DAY=12       # Overall daily cap across all symbols
SIGNAL_COOLDOWN_MINUTES=30  # Minutes before the same signal can re-trigger
MIN_SPREAD_COST=0.10        # Skip spreads below this cost (liquidity filter)

# Chop guards
ADX_SLOPE_BARS=10                # Entry requires ADX rising over last N bars (0=off)
ORB_BREAKOUT_BUFFER_PCT=0.001    # Breakout must clear ORB level by this fraction (0=off)
VWAP_INVALIDATION_BARS=3         # Exit if price closes past VWAP N bars in a row (0=off)
MAX_INVALIDATIONS_PER_SIGNAL=2   # Stand down a signal after N invalidation exits/day (0=off)

# Conviction sizing
CONVICTION_SIZING_ENABLED=true   # Score entries 0-5 and size the budget by tier
CONVICTION_LOW_MULT=0.5          # Budget multiplier when score <= 1
CONVICTION_HIGH_MULT=1.5         # Budget multiplier when score >= 4
MIN_CONVICTION_SCORE=2           # Skip entries scoring below this (-99 to disable)

# Take-profit target
TAKE_PROFIT_TARGET_PCT=0.60      # Resting limit sell at entry x 1.60 on fill (0=off)

# Fast exit polling (fixes exit sampling slippage; 0 disables)
FAST_POLL_SECONDS=15             # Loop cadence while an exit needs tight watching
FAST_POLL_ARM_PCT=0.35           # Profit level that switches to fast polling

# Risk management
TAKE_PROFIT_TRAIL_TRIGGER=0.50   # Trailing stop arms only after the trade peaks here (+50%)
TRAILING_STOP_LOSS_PCT=0.10      # Once armed, exit if profit falls to (1 - this) of the peak (90%)
HARD_STOP_LOSS_PCT=0.70          # Exit immediately if spread loses this much
MAX_CONSECUTIVE_LOSSES=5         # Circuit breaker threshold
MAX_DAILY_LOSS=400               # Stop entries once day's realized net P&L <= -$400 (0=off)
EOD_FLATTEN_TIME=15:55           # Force-close all positions at this ET time (before the 4 PM close)

# Optional
DISCORD_WEBHOOK_URL=
```

---

## Running the Bot

With IB Gateway open and logged into your paper account:

```bash
python src/main.py
```

Expected startup output:
```
Connecting to IBKR at 127.0.0.1:4002 (clientId=1)
Connected to IBKR
Starting 0DTE Options Spread Trading Bot (IBKR)...
Daily trade count reset for 2026-05-22
Market closed. Next open in 14h 22m. Sleeping 1 hour.
```

The bot sleeps intelligently when the market is closed — it calculates the exact time to the next 9:30 AM EST weekday open and wakes hourly overnight to keep the IBKR connection healthy.

---

## Discord Alerts

Configure `DISCORD_WEBHOOK_URL` in `.env` to receive real-time trade notifications:

| Alert | Colour | Trigger |
|-------|--------|---------|
| ⏳ ORDER SUBMITTED | 🟠 Orange | Immediately when the BAG order is sent to IBKR (fires even if later rejected) — includes the conviction score and sized budget |
| 📋 TODAY | 🟢/🔴 by net | After every new trade — snapshot of all open positions (live P&L), closed trades (realized P&L), and the running net |
| 🟢 NEW ENTRY FILLED | 🟢 Green | When IBKR confirms the order is filled — includes strikes, price, indicators |
| 🔵 POSITION CLOSED | 🔵 Blue (profit) / 🔴 Red (loss) | When IBKR **confirms the closing fill** — actual fill price, P&L, round-trip commissions, net after fees |
| ⚠️ POSITION CLOSED EXTERNALLY | 🟠 Orange | When the bot detects a tracked position is no longer in your IBKR account (closed manually via Client Portal, mobile, or TWS) — drops it from tracking |
| 🔁 ADOPTED ORPHANED POSITIONS | 🟠 Orange | At startup, when the account holds open 0DTE spreads the bot wasn't tracking (e.g. after a restart) — they're adopted and managed by the normal exit rules |
| ⛔ SIGNAL THROTTLED | ⚪ Grey | When a symbol+direction hits N **losing** thesis-invalidation exits (< −10%) in a day — no re-entries on that signal until tomorrow. Profitable invalidations don't count |
| 🛑 DAILY LOSS LIMIT | 🔴 Red | When the day's realized P&L (net of fees) breaches −`MAX_DAILY_LOSS` — no new entries today; open positions still managed |
| 🚨 CIRCUIT BREAKER | 🔴 Red | After N consecutive losing trades — no more entries today |
| 📅 DAY SUMMARY | 🟢/🔴 by net | Once after the market closes — gross P&L, **commissions, net after fees**, win/loss count, win rate, per-trade breakdown |

---

## Watchlist

The bot trades **SPY, QQQ, and IWM** — three liquid ETFs with genuine daily 0DTE expirations and low correlation to each other:

| Symbol | Index | Characteristic |
|--------|-------|----------------|
| SPY | S&P 500 | Broad market anchor; deepest options liquidity |
| QQQ | Nasdaq 100 | Tech-heavy; diverges from SPY on sector rotations |
| IWM | Russell 2000 | Small caps; most independent signal, rate-sensitive |

All three are American-style ETF options with $1 strike steps and $1 spread width.

---

## Strategy

### Indicators (calculated on 1-minute bars from IBKR)

- **VWAP** — Volume-Weighted Average Price anchored to 9:30 AM EST each day
- **30-Minute ORB** — Opening Range Breakout using the high/low of the 9:30–10:00 AM window, always anchored to wall-clock time (not the first bar), so mid-day restarts work correctly
- **ADX(14)** — Trend strength; must exceed 25 to consider any entry
- **$TICK / $VOLD** — NYSE market breadth indices fetched from IBKR every loop (cached 60s across all symbols). The reading is logged to `audit.csv` alongside every trade so you can analyse whether diverging breadth correlates with losses — but it does **not** block entries. Once paper-trade data shows a real correlation, it can be promoted to a hard filter.

### Entry Conditions

**CALL (bullish):** ADX > 25 **and rising** AND price > VWAP AND price > ORB High × (1 + buffer)

**PUT (bearish):** ADX > 25 **and rising** AND price < VWAP AND price < ORB Low × (1 − buffer)

### Entry Filters (checked in order)

1. **Market hours** — 9:30 AM–4:00 PM EST weekdays only
2. **Entry window** — No new entries after 3:00 PM EST
3. **Circuit breaker** — Halts all entries if N consecutive losses were hit
4. **Daily trade cap** — Hard ceiling of `MAX_TRADES_PER_DAY` (default 12)
5. **Signal cooldown** — After a trade, that symbol+direction is locked for `SIGNAL_COOLDOWN_MINUTES` (default 30). Once the cooldown expires the signal can re-trigger, enabling continuation trades on trending days
6. **One active trade per symbol** — Cannot open a second SPY trade while one is already running
7. **ADX rising (chop guard)** — ADX must have increased over the last `ADX_SLOPE_BARS` (default 10) bars. A level check passes on residual momentum; the slope confirms the trend is still alive. Fails open early in the session when the lookback is not yet computable
8. **Breakout buffer (chop guard)** — Price must clear the ORB level by `ORB_BREAKOUT_BUFFER_PCT` (default 0.1%), filtering micro-poke false breakouts
9. **Invalidation throttle (chop guard)** — After `MAX_INVALIDATIONS_PER_SIGNAL` (default 2) thesis-invalidation exits on the same symbol+direction in one day, that signal stands down until tomorrow — the market has proven it chop. A ⛔ Discord alert fires when the throttle trips
10. **Minimum spread cost** — Spread must cost ≥ `MIN_SPREAD_COST` (default $0.10) for liquidity

### Position Sizing (conviction-based)

Every entry is scored **0–5** from signals already computed:

| +1 point each | −1 point each |
|---|---|
| ADX ≥ 30 (strong trend, not marginal) | Per thesis-invalidation exit already taken today (any signal) |
| ADX slope ≥ +3 (steeply rising) | |
| ≥1 other symbol leaning the same direction (cross-symbol agreement) | |
| Entry before 11:00 ET (open drive, not midday) | |
| Calm tape: ≤ 4 VWAP crosses so far today | |

The score sets the position budget — and below `MIN_CONVICTION_SCORE` (default 2) the bot **doesn't trade at all**: LOW-tier trades ran 1W/5L and tiny positions can't clear the per-contract fee floor (one +$13 gross "winner" lost money after $13.87 commissions).

| Score | Tier | Action (at `MAX_POSITION_SIZE=300`) |
|-------|------|--------------------------------------|
| ≤ 1 | LOW | **Skip — no trade** (set `MIN_CONVICTION_SCORE=-99` to size at $150 instead) |
| 2–3 | MEDIUM | $300 (1.0×) |
| ≥ 4 | HIGH | $450 (1.5×) |

```
contracts = floor(budget / (spread_cost × 100))
```

The score and its component breakdown are logged, written to `audit.csv` (`Conviction` column), and included in every Discord entry alert. Set `CONVICTION_SIZING_ENABLED=false` to revert to flat sizing.

---

## Risk Management

Four exit mechanisms:

| Rule | Condition | Notes |
|------|-----------|-------|
| **Take-Profit Target** | Resting limit sell at entry × 1.60, parked the moment the entry fills | Fills between heartbeats and sells into strength. Max peak ever recorded is +64.6% — waiting for +100% holds gamma risk for value that only exists at expiry. All other exit paths cancel it first |
| **Hard Stop Loss** | Spread loses ≥ 70% of entry value | Immediate exit; the catastrophic backstop |
| **Thesis Invalidation** | Price closes on the wrong side of VWAP for `VWAP_INVALIDATION_BARS` (default 3) consecutive 1-min bars | The entry reason was "price beyond VWAP + ORB" — when that's gone, exit instead of riding to −70%. On 2026-07-01 this would have cut three −71/−74% losers near −20/−30% |
| **Trailing Stop** | Arms only after the trade peaks at +50%; then exits if profit falls to 90% of the peak (e.g. peak +50% → exit +45%) | Lets winners run, then locks them in |

> The thesis-invalidation rule replaced the old "no protection between 0% and +50%" gap: a losing trade now exits when its entry conditions die, not only at −70%. Set `VWAP_INVALIDATION_BARS=0` to disable and restore the old behaviour.

### End-of-Day Flatten

These are 0DTE options — they expire worthless or get assigned at the close. At `EOD_FLATTEN_TIME` (default **3:55 PM ET**), the bot force-closes every open position regardless of P&L, so nothing is ever held into expiry. Unfilled entry orders lingering near the close are cancelled. New entries already stop at 3:00 PM, so nothing reopens.

### Circuit Breaker

After `MAX_CONSECUTIVE_LOSSES` (default 5) losing trades **in a row**, the bot stops placing new entries for the rest of the day. A Discord alert fires immediately. The counter resets at midnight. A single winning trade between losses resets the counter to zero.

### Daily Loss Limit

The backstop beneath every other guard: once the day's **realized P&L net of commissions** breaches −`MAX_DAILY_LOSS` (default $400), no new entries are placed until tomorrow — regardless of what the signals say. Open positions remain managed by the exit rules and the EOD flatten. A 🛑 Discord alert fires the moment the limit is breached. Unlike the circuit breaker (which needs consecutive losses), this catches death-by-many-cuts days where wins are interleaved but the net bleeds.

---

## Audit Log

Every fill (entry and exit) is appended to `audit.csv`:

| Column | Description |
|--------|-------------|
| Timestamp | When the trade executed |
| Action | BUY or SELL |
| Symbol | SPY, QQQ, or IWM |
| Direction | CALL or PUT |
| Price | Entry/exit spread mid-price |
| Underlying_Price | Spot price at trade time |
| ADX | Trend strength reading |
| VWAP | VWAP at trade time |
| ORB_High | 30-min opening range high |
| ORB_Low | 30-min opening range low |
| Breadth | $TICK/$VOLD annotation at entry (BUY rows) |
| Reason | Entry signal or exit rule that fired |
| Profit_Pct | P&L % (SELL rows only) |
| Dollar_PnL | Dollar P&L (SELL rows only) |
| ADX_Slope | ADX change over the slope-lookback window at entry (BUY rows) |
| Peak_Pct | Highest profit % the trade reached before exit (SELL rows only) |
| Conviction | Sizing score at entry, e.g. `HIGH 4/5 \| ADX✓ slope✓ agree✓(QQQ) early✓ tape✗(6x)` (BUY rows) |
| Commission | IBKR-reported commissions — entry legs on BUY rows; full round trip on SELL rows |

> Exit prices are **IBKR-confirmed fills** (`avgFillPrice`), not submission prices. Unfilled closing orders are repriced after 3 minutes.

> Timestamps are logged in **ET** (rows before 2026-07-05 are in the machine's local time, CDT).

---

## Dashboard

`dashboard.xlsx` is regenerated automatically **after each trading day** — right after the 📅 day summary is sent to Discord. It can also be rebuilt manually anytime:

```bash
python scripts/build_dashboard.py
```

Three sheets, built from `audit.csv` with live Excel formulas:

| Sheet | Contents |
|-------|----------|
| **Summary** | KPIs (total P&L, win rate, avg win/loss, profit factor, best/worst day) + daily P&L bars + equity curve |
| **Analysis** | P&L by symbol, by exit rule (what each rule costs/saves), and by entry hour (ET) — each with a chart |
| **Trades** | Full paired ledger: entry/exit, hold time, ADX + slope at entry, peak %, P&L, exit rule, orphan flags |

---

## IBKR Notes

- **Delayed data** — The bot requests market data type 4 (15-min delayed) on connect. No live data subscription is required for paper trading.
- **Informational IBKR codes** — Codes like 162 (no data yet), 2104/2106 (farm connected), 10091/10167 (delayed data notice) are suppressed from logs and handled silently. Real errors (order rejections, etc.) still appear as `WARNING`.
- **Auto-reconnect** — If the IBKR connection drops mid-session, the bot attempts to reconnect at the start of the next loop iteration.
- **Position reconciliation** — Each loop the bot checks every tracked position against your actual IBKR account (`ib.positions()`). If you close a spread manually (Client Portal, mobile app, or TWS), the bot detects the missing position (after two consecutive checks, with a 90-second grace period after entry) and drops it from tracking — so it never tries to manage or re-sell a position you no longer hold. A ⚠️ alert fires. P&L for an externally-closed trade is **not** recorded, since the bot doesn't know the price you exited at.
- **Startup adoption** — On start, the bot scans `ib.positions()` for open 0DTE option spreads it isn't tracking (orphaned by a restart), reconstructs them (entry price estimated from account `avgCost`), and manages them with the normal exit rules and EOD flatten. Unpairable or non-0DTE positions trigger a ⚠️ alert for manual review instead.
- **Stale-feed detector** — If the latest intraday bar is more than 10 minutes old during market hours, a WARNING is logged (indicators may be unreliable).

For a full explanation of the bot's internal logic, see [How It Works](docs/HOW_IT_WORKS.md).
