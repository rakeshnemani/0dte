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

# Risk management
TAKE_PROFIT_TRAIL_TRIGGER=0.40   # Activate trailing stop once up this much
TRAILING_STOP_LOSS_PCT=0.10      # Trail by this amount from the peak
MAX_PROFIT_EXIT_MULTIPLIER=0.70  # Exit if profit drops to 70% of the all-time peak
HARD_STOP_LOSS_PCT=0.70          # Exit immediately if spread loses this much
MAX_CONSECUTIVE_LOSSES=5         # Circuit breaker threshold

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
| 🟢 NEW ENTRY FILLED | 🟢 Green | When IBKR confirms the order is filled — includes strikes, price, indicators |
| 🔵 POSITION CLOSED | 🔵 Blue (profit) / 🔴 Red (loss) | When the closing order is submitted — includes P&L and exit reason |
| 🚨 CIRCUIT BREAKER | 🔴 Red | After N consecutive losing trades — no more entries today |

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

**CALL (bullish):** ADX > 25 AND price > VWAP AND price > ORB High

**PUT (bearish):** ADX > 25 AND price < VWAP AND price < ORB Low

### Entry Filters (checked in order)

1. **Market hours** — 9:30 AM–4:00 PM EST weekdays only
2. **Entry window** — No new entries after 3:00 PM EST
3. **Circuit breaker** — Halts all entries if N consecutive losses were hit
4. **Daily trade cap** — Hard ceiling of `MAX_TRADES_PER_DAY` (default 12)
5. **Signal cooldown** — After a trade, that symbol+direction is locked for `SIGNAL_COOLDOWN_MINUTES` (default 30). Once the cooldown expires the signal can re-trigger, enabling continuation trades on trending days
6. **One active trade per symbol** — Cannot open a second SPY trade while one is already running
7. **Minimum spread cost** — Spread must cost ≥ `MIN_SPREAD_COST` (default $0.10) for liquidity

### Position Sizing

```
contracts = floor(MAX_POSITION_SIZE / (spread_cost × 100))
```

At `MAX_POSITION_SIZE=$300` and a $0.50 spread: 6 contracts = $300 max risk per trade.

---

## Risk Management

Three layered exit rules, checked every 60 seconds:

| Rule | Condition | Notes |
|------|-----------|-------|
| **Hard Stop Loss** | Spread loses ≥ 70% of entry value | Immediate exit; aggressive — standard is 50% |
| **Max Profit Trail** | Profit drops to ≤ 70% of peak profit seen | Protects gains once a position has been profitable |
| **Trailing Stop** | Profit drops 10% from peak, after reaching 40%+ | Locks in gains on strong moves |

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
| Reason | Entry signal or exit rule that fired |
| Profit_Pct | P&L % (SELL rows only) |
| Dollar_PnL | Dollar P&L (SELL rows only) |

---

## IBKR Notes

- **Delayed data** — The bot requests market data type 4 (15-min delayed) on connect. No live data subscription is required for paper trading.
- **Informational IBKR codes** — Codes like 162 (no data yet), 2104/2106 (farm connected), 10091/10167 (delayed data notice) are suppressed from logs and handled silently. Real errors (order rejections, etc.) still appear as `WARNING`.
- **Auto-reconnect** — If the IBKR connection drops mid-session, the bot attempts to reconnect at the start of the next loop iteration.

For a full explanation of the bot's internal logic, see [How It Works](docs/HOW_IT_WORKS.md).
