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

## Prerequisites

### 1. Install IB Gateway (or TWS)

The bot connects to IBKR via a local socket — IB Gateway must be running before you start the bot.

1. Download **IB Gateway** (lighter than full TWS): https://www.interactivebrokers.com/en/trading/ibgateway-stable.php
2. Log in with your **Paper Trading** account credentials (separate from your live account — find them in IBKR Account Management)
3. Go to **Configure → Settings → API → Settings** and configure:
   - Check **Enable ActiveX and Socket Clients**
   - Uncheck **Read-Only API**
   - Socket port: `4002`
   - Add `127.0.0.1` to Trusted IP Addresses
4. Keep IB Gateway open whenever the bot runs

> If you prefer full TWS over IB Gateway, use port `7497` and set `IBKR_PORT=7497` in `.env`.

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure `.env`

The `.env` file is pre-configured for IB Gateway paper mode on localhost. Change values if needed:

```env
IBKR_HOST=127.0.0.1
IBKR_PORT=4002          # 4002 = IB Gateway paper, 7497 = TWS paper
IBKR_CLIENT_ID=1

SYMBOLS=SPY,SPX
MAX_POSITION_SIZE=300.0
MAX_TRADES_PER_DAY=5
MIN_SPREAD_COST=0.10

TAKE_PROFIT_TRAIL_TRIGGER=0.40
TRAILING_STOP_LOSS_PCT=0.10
MAX_PROFIT_EXIT_MULTIPLIER=0.70
HARD_STOP_LOSS_PCT=0.50

DISCORD_WEBHOOK_URL=     # optional
```

## Running the Bot

With IB Gateway open and logged into your paper account:

```bash
python src/main.py
```

You should see `Connected to IBKR` in the logs. The bot then enters its 60-second heartbeat loop.

## Audit Log

Every trade (entry and exit) is appended to `audit.csv`:

| Column | Description |
|--------|-------------|
| Timestamp | When the trade executed |
| Action | BUY or SELL |
| Symbol | Underlying (SPY, SPX, etc.) |
| Direction | CALL or PUT |
| Price | Entry/exit spread price |
| Underlying_Price | Spot price at trade time |
| ADX | Trend strength at trade time |
| VWAP | Volume-Weighted Average Price |
| ORB_High | 30-min opening range high |
| ORB_Low | 30-min opening range low |
| Reason | Why the trade was entered or exited |
| Profit_Pct | P&L percentage (SELL rows only) |
| Dollar_PnL | Dollar P&L (SELL rows only) |

## Strategy

The bot uses 1-minute bars from IBKR to calculate:
- **VWAP** — Volume-Weighted Average Price (institutional cost basis)
- **30-Minute ORB** — Opening Range Breakout window (9:30–10:00 AM EST), anchored to fixed wall-clock time so bot restarts mid-day don't break the signal
- **ADX** — Trend strength (must be > 25 to enter)

ATM debit spreads are executed as IBKR BAG combo orders.

### Entry Filters

- **Time-of-Day:** No new entries after 3:00 PM EST
- **Daily Trade Limit:** Max 5 trades per day (configurable, resets at market open)
- **Minimum Spread Cost:** Spread must cost ≥ $0.10 to ensure liquidity
- **Trend Filter:** ADX > 25
- **Price Action:** Price must break above ORB High (CALL) or below ORB Low (PUT), and be on the correct side of VWAP

### Per-Symbol Concurrency

One active trade per symbol at a time. Multiple symbols can have simultaneous open positions (e.g., one SPY trade and one SPX trade).

### Risk Management

1. **Hard Stop Loss:** Exit if spread loses 50% of entry value
2. **Max Profit Trailing Exit:** Exit if profit drops to 70% of the peak profit ever seen
3. **Trailing Stop:** Once profit reaches 40%, a 10% trailing stop activates

## IBKR-Specific Notes

- **SPX 0DTE options** are routed as `SPXW` (weekly series) — the bot handles this mapping automatically
- **SPX historical data** is fetched as `MIDPOINT` (cash index, no trade volume); a dummy volume of 1 is used so VWAP math remains valid
- **Market data type** is set to delayed (type 4) on connect, so the bot works without live data subscriptions on a paper account
- The bot uses `ib_insync` for all IBKR communication — IB Gateway or TWS must remain open for the duration of the session

For more details, see [How It Works](docs/HOW_IT_WORKS.md).
