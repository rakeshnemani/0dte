# 0dte Paper Trading Bot

A simple algorithmic trading bot designed to paper trade 0DTE options spreads based on technical indicators (VWAP, ADX, 30-Min ORB).

## Project Structure

```text
0dte/
├── .env                # Environment variables and configuration
├── .gitignore          # Ignored files
├── README.md           # Project documentation
├── requirements.txt    # Python dependencies
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

## Strategy
The bot uses `pandas` and `ta` to fetch intraday 1-minute bars and calculates:
- VWAP
- 30-Minute ORB (Opening Range Breakout)
- ADX (Trend strength)

It executes ATM spreads and manages risk via a strict set of trailing stop and hard stop loss configurations defined in the `.env` file.
