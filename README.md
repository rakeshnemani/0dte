# 0DTE Paper Trading Bot

A Python algorithmic trading bot that paper trades 0DTE options spreads on Interactive Brokers (IBKR) using VWAP, ADX, and 30-minute Opening Range Breakout signals.

## Project Structure

```text
0dte/
├── .env                # Environment variables and configuration
├── .gitignore
├── README.md
├── requirements.txt    # Python dependencies
├── audit.csv           # Trade log (auto-created on first run)
├── docs/
│   └── HOW_IT_WORKS.md # Deep-dive on bot logic and design decisions
└── src/
    ├── __init__.py
    ├── config.py       # Configuration loader
    ├── bot.py          # TradingBot class — strategy + IBKR broker calls
    └── main.py         # Entry point
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

# Risk management
TAKE_PROFIT_TRAIL_TRIGGER=0.50   # Trailing stop arms only after the trade peaks here (+50%)
TRAILING_STOP_LOSS_PCT=0.10      # Once armed, exit if profit falls to (1 - this) of the peak (90%)
HARD_STOP_LOSS_PCT=0.70          # Exit immediately if spread loses this much
MAX_CONSECUTIVE_LOSSES=5         # Circuit breaker threshold
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
| ⏳ ORDER SUBMITTED | 🟠 Orange | Immediately when the BAG order is sent to IBKR (fires even if later rejected) |
| 📋 TODAY | 🟢/🔴 by net | After every new trade — snapshot of all open positions (live P&L), closed trades (realized P&L), and the running net |
| 🟢 NEW ENTRY FILLED | 🟢 Green | When IBKR confirms the order is filled — includes strikes, price, indicators |
| 🔵 POSITION CLOSED | 🔵 Blue (profit) / 🔴 Red (loss) | When the closing order is submitted — includes P&L and exit reason |
| ⚠️ POSITION CLOSED EXTERNALLY | 🟠 Orange | When the bot detects a tracked position is no longer in your IBKR account (closed manually via Client Portal, mobile, or TWS) — drops it from tracking |
| 🔁 ADOPTED ORPHANED POSITIONS | 🟠 Orange | At startup, when the account holds open 0DTE spreads the bot wasn't tracking (e.g. after a restart) — they're adopted and managed by the normal exit rules |
| 🚨 CIRCUIT BREAKER | 🔴 Red | After N consecutive losing trades — no more entries today |
| 📅 DAY SUMMARY | 🟢/🔴 by net | Once after the market closes — total realized P&L, win/loss count, win rate, per-trade breakdown |

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
9. **Minimum spread cost** — Spread must cost ≥ `MIN_SPREAD_COST` (default $0.10) for liquidity

### Position Sizing

```
contracts = floor(MAX_POSITION_SIZE / (spread_cost × 100))
```

At `MAX_POSITION_SIZE=$300` and a $0.50 spread: 6 contracts = $300 max risk per trade.

---

## Risk Management

Three exit rules, checked every 60 seconds:

| Rule | Condition | Notes |
|------|-----------|-------|
| **Hard Stop Loss** | Spread loses ≥ 70% of entry value | Immediate exit; the catastrophic backstop |
| **Thesis Invalidation** | Price closes on the wrong side of VWAP for `VWAP_INVALIDATION_BARS` (default 3) consecutive 1-min bars | The entry reason was "price beyond VWAP + ORB" — when that's gone, exit instead of riding to −70%. On 2026-07-01 this would have cut three −71/−74% losers near −20/−30% |
| **Trailing Stop** | Arms only after the trade peaks at +50%; then exits if profit falls to 90% of the peak (e.g. peak +50% → exit +45%) | Lets winners run, then locks them in |

> The thesis-invalidation rule replaced the old "no protection between 0% and +50%" gap: a losing trade now exits when its entry conditions die, not only at −70%. Set `VWAP_INVALIDATION_BARS=0` to disable and restore the old behaviour.

### End-of-Day Flatten

These are 0DTE options — they expire worthless or get assigned at the close. At `EOD_FLATTEN_TIME` (default **3:55 PM ET**), the bot force-closes every open position regardless of P&L, so nothing is ever held into expiry. Unfilled entry orders lingering near the close are cancelled. New entries already stop at 3:00 PM, so nothing reopens.

### Circuit Breaker

After `MAX_CONSECUTIVE_LOSSES` (default 5) losing trades **in a row**, the bot stops placing new entries for the rest of the day. A Discord alert fires immediately. The counter resets at midnight. A single winning trade between losses resets the counter to zero.

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
