# 0dte Paper Trading Bot

A simple algorithmic trading bot designed to paper trade 0DTE options spreads based on technical indicators (VWAP, ADX, 30-Min ORB).

## Project Structure

```text
0dte/
├── .env                # Environment variables and configuration
├── .gitignore          # Ignored files
├── README.md           # Project documentation
├── requirements.txt    # Python dependencies
├── docs/
│   └── HOW_IT_WORKS.md # Detailed explanation of the bot's logic
└── src/
    ├── __init__.py
    ├── config.py       # Configuration loader
    ├── bot.py          # Trading Bot class and logic
    └── main.py         # Entry point to run the bot
```

## Setup

1. Make sure you have python installed.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Update the `.env` file with your specific Alpaca Paper API keys. By default, it uses the ones provided during setup.

## Running the Bot

Run the following command to start monitoring the market:
```bash
python src/main.py
```

The bot will log all trades to `audit.csv` with full indicator context for easy review and strategy tuning.

## Audit Log

The bot maintains an `audit.csv` file that captures every trade with full context:

| Column | Description |
|--------|-------------|
| Timestamp | When the trade was executed |
| Action | BUY or SELL |
| Symbol | Underlying symbol (SPY, SPX, etc.) |
| Direction | CALL or PUT |
| Price | Entry/exit spread price |
| Underlying_Price | Spot price of underlying at trade time |
| ADX | Average Directional Index (trend strength) |
| VWAP | Volume-Weighted Average Price |
| ORB_High | 30-minute Opening Range high |
| ORB_Low | 30-minute Opening Range low |
| Reason | Signal reason (bullish breakout, hard stop loss, etc.) |
| Profit_Pct | Profit/loss percentage (SELL rows only) |
| Dollar_PnL | Dollar profit/loss amount (SELL rows only) |

This detailed logging enables quick review of trades and helps tune the strategy by analyzing which market conditions lead to winners vs. losers.

## Strategy
The bot uses `pandas` and `ta` to fetch intraday 1-minute bars and calculates:
- **VWAP** (Volume-Weighted Average Price)
- **30-Minute ORB** (Opening Range Breakout from 9:30 AM - 10:00 AM EST)
- **ADX** (Trend strength indicator)

It executes ATM spreads and manages risk via a strict set of trailing stop loss configurations defined in the `.env` file.

### Entry Filters
- **Time-of-Day Filter:** No entries after 1:00 PM EST (13:00). This cutoff is checked at the loop level before any API calls, preventing unnecessary market data fetches. Protects against late-day theta decay.
- **Daily Trade Limit:** Maximum 5 trades opened per day (resets at 9:30 AM EST). Prevents over-trading during choppy days.
- **Minimum Spread Cost:** Spread must cost at least $0.10 (configurable via `MIN_SPREAD_COST`). Prevents trading nearly-worthless spreads with no liquidity.
- **Trend Filter:** ADX must be > 25 to indicate a strong directional trend.
- **Price Action Filter:** Price must break above/below the 30-minute opening range AND be on the correct side of VWAP.

### Per-Symbol Concurrency
The bot maintains a **maximum of one active trade per symbol**. This allows simultaneous positions across multiple symbols (e.g., one SPY trade + one SPX trade at the same time) while preventing overexposure to a single underlying.

### Risk Management Rules
1. **Hard Stop Loss:** Exit if spread loses 50% of entry value
2. **Max Profit Trailing Exit:** Exit if profit drops to 70% of the peak profit
3. **Trailing Stop:** If profit reaches 40%, lock in a permanent 10% trailing stop

For more details, see [How It Works](docs/HOW_IT_WORKS.md).
