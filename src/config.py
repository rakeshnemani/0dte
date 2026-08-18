import os
from dotenv import load_dotenv

load_dotenv()

# IBKR connection settings
IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "4002"))        # 4002 = IB Gateway paper, 7497 = TWS paper
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))
# Market data type: 1 = live (real-time), 2 = frozen, 3 = delayed, 4 = delayed-frozen.
# Default 1 — requires active data subscriptions (paper inherits the live account's).
IBKR_MARKET_DATA_TYPE = int(os.getenv("IBKR_MARKET_DATA_TYPE", "1"))

SYMBOLS = os.getenv("SYMBOLS", "SPX").split(",")
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "200.0"))   # budget shown in the Discord submit alert

# ── Strategy selector ────────────────────────────────────────────────────────
# Two single-leg directional strategies, run together (STRATEGY="trend,gex").
#   trend = Supertrend(7,3) + PSAR(0.02,0.2) + Kaufman-chop flip, inside TREND_WINDOWS.
#   gex   = dealer gamma-flip momentum (negative-γ / wall breakout + 15-min OR breakout).
# Each holds its own position per symbol (trades keyed strategy:symbol); account-level
# guards (cooldown, circuit breaker, daily loss, trade count) are shared. Both buy ONE
# ATM (~50Δ) option — CALL bullish, PUT bearish. No spreads, no condors anywhere.
STRATEGY = os.getenv("STRATEGY", "trend,gex")
ACTIVE_STRATEGIES = [s.strip() for s in STRATEGY.split(",") if s.strip()]

# ── Trend strategy (STRATEGY='trend') ────────────────────────────────────────
# Entry = Supertrend flip INTO a direction AND PSAR agrees AND kaufman-chop <= TREND_KAUF_MAX,
# only inside a TREND_WINDOWS slot. Exits: hard stop (HARD_STOP_LOSS_PCT) + Supertrend
# reversal + EOD flatten. Built on a 3-year SPX backtest (scripts/backtest_spread_dollars.py).
TREND_SUPERTREND_PERIOD = int(os.getenv("TREND_SUPERTREND_PERIOD", "7"))
TREND_SUPERTREND_MULT = float(os.getenv("TREND_SUPERTREND_MULT", "3.0"))
TREND_KAUF_N = int(os.getenv("TREND_KAUF_N", "14"))
TREND_KAUF_MAX = float(os.getenv("TREND_KAUF_MAX", "50"))
TREND_MIN_BARS = int(os.getenv("TREND_MIN_BARS", "20"))     # session warmup before signalling
# Entry windows (ET), comma-separated HH:MM-HH:MM. 2026-08-09: drop the power hour
# (theta kills naked longs late) — trade the open + midday until 2 PM.
TREND_WINDOWS = os.getenv("TREND_WINDOWS", "09:30-14:00")
# Skip an entry when entry-time realized vol (open→now, annualized, NO lookahead) is below
# this — a quiet-so-far day starves a naked long (it just bleeds theta). 0 disables.
TREND_SKIP_LOWIV = float(os.getenv("TREND_SKIP_LOWIV", "0.082"))

# ── GEX strategy (STRATEGY='gex') — dealer gamma-flip momentum ────────────────
# FORWARD-TEST ONLY (no historical GEX data exists for free). Entry = negative-gamma regime
# (SPX < Gflip, OR breaking a concentration zone) + 15-min opening-range breakout + short-term
# momentum, inside a GEX_WINDOWS slot. GEX math lives in src/gex.py (unit-tested); the live
# chain (OI + IV per strike) is pulled from IBKR and saved to data/gex/ for future backtesting.
GEX_WINDOWS = os.getenv("GEX_WINDOWS", "09:30-15:55")                 # OR-breakout gates entries to post-9:45
GEX_OR_MINUTES = int(os.getenv("GEX_OR_MINUTES", "15"))               # opening range = 9:30 + this many min
GEX_FLATTEN_TIME = os.getenv("GEX_FLATTEN_TIME", "15:55")             # flatten all positions by 3:55 PM
# GEX exit philosophy (2026-08-17): LET THE CONVEX TAIL RIDE. No invalidation cut and no fixed
# max-loss stop — those cut the 08-17 winner at -4% before it ran to +100%. Exit only via a
# trailing stop (arms once peaked at GEX_TRAIL_TRIGGER, exits on giving back GEX_TRAIL_GIVEBACK
# OF the peak) + a WIDE catastrophe backstop so a trade that never peaks can't ride to a
# full-premium loss. GEX_TAKE_PROFIT>0 re-enables a hard TP (off by default — the tail is the edge).
GEX_TAKE_PROFIT = float(os.getenv("GEX_TAKE_PROFIT", "0.0"))
GEX_TRAIL_TRIGGER = float(os.getenv("GEX_TRAIL_TRIGGER", "0.50"))         # arm once peaked +50%
GEX_TRAIL_GIVEBACK = float(os.getenv("GEX_TRAIL_GIVEBACK", "0.20"))       # exit at 80% of peak
GEX_CATASTROPHE_STOP = float(os.getenv("GEX_CATASTROPHE_STOP", "0.80"))   # 0 = no stop at all
# Theta protection (2026-08-11): skip a GEX entry when entry-time realized vol (open→now,
# annualized, NO lookahead) < this — a slow tape can't move fast enough for a naked leg to
# outrun theta. 0 disables.
GEX_SKIP_LOWIV = float(os.getenv("GEX_SKIP_LOWIV", "0.082"))
GEX_MOMENTUM_BARS = int(os.getenv("GEX_MOMENTUM_BARS", "2"))          # price momentum over N bars ("delta acceleration")
GEX_WALL_TOL_PCT = float(os.getenv("GEX_WALL_TOL_PCT", "0.0015"))     # "at a wall" tolerance (~0.15% of spot)
GEX_CHAIN_STRIKE_PCT = float(os.getenv("GEX_CHAIN_STRIKE_PCT", "0.05"))  # fetch strikes within ±5% of spot
GEX_CHAIN_EXPIRIES = int(os.getenv("GEX_CHAIN_EXPIRIES", "3"))        # nearest N expirations to include
GEX_CHAIN_MAX_STRIKES = int(os.getenv("GEX_CHAIN_MAX_STRIKES", "50")) # cap strikes nearest ATM (data-line budget)
GEX_REFRESH_MIN = int(os.getenv("GEX_REFRESH_MIN", "30"))            # re-fetch OI chain every N min (OI is ~static intraday)

# ── Shared entry/order settings ──────────────────────────────────────────────
# Each (symbol, direction) cools down for SIGNAL_COOLDOWN_MINUTES after a trade, then can
# re-trigger. MAX_TRADES_PER_DAY is the overall daily cap across all symbols/strategies.
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "12"))
SIGNAL_COOLDOWN_MINUTES = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "30"))
# Skip a trade if the option mid is below this (a FEE floor, not a risk knob — a cheap
# option buys the most contracts and therefore the most per-contract fees).
MIN_SPREAD_COST = float(os.getenv("MIN_SPREAD_COST", "0.30"))
# #34: an entry limit has a shelf life measured in bars, not hours — a resting limit only
# fills once the option decays to our bid (the market moved AGAINST the thesis). Cancel an
# unfilled entry after this many seconds and re-evaluate fresh. 0 disables (wait forever).
ENTRY_ORDER_TIMEOUT_SECONDS = int(os.getenv("ENTRY_ORDER_TIMEOUT_SECONDS", "120"))
# Price the entry limit this fraction of the way from mid → ask: 0 = mid, 0.5 = (mid+ask)/2,
# 1 = ask. A passive mid bid doesn't fill on a moving tape; crossing a little buys the fill.
ENTRY_AGGRESSION = float(os.getenv("ENTRY_AGGRESSION", "0.5"))
STRIKE_STEP = {"SPY": 1, "QQQ": 1, "IWM": 1, "XSP": 1, "SPX": 5}   # SPX 0DTE lists 5-pt strikes ATM

# Signal source: index products have thin/no-volume 1-min bars, so the exit-audit's
# VWAP/ADX/ORB are computed from a liquid proxy (SPY). Strikes + orders still use the
# execution symbol. A symbol absent from this map sources its own bars.
SIGNAL_SOURCE = {"XSP": "SPY", "SPX": "SPY"}

# ── Exit / risk guards (shared) ──────────────────────────────────────────────
# Fast exit polling: the loop runs every 60s normally but drops to FAST_POLL_SECONDS when an
# exit needs tight watching — a closing order in flight, or an ACTIVE trade with profit >=
# FAST_POLL_ARM_PCT (approaching a trail trigger). Fixes sampling slippage on fast moves.
FAST_POLL_SECONDS = int(os.getenv("FAST_POLL_SECONDS", "15"))
FAST_POLL_ARM_PCT = float(os.getenv("FAST_POLL_ARM_PCT", "0.35"))
# Trend hard stop (GEX uses its own GEX_CATASTROPHE_STOP instead). .env runs 0.50.
HARD_STOP_LOSS_PCT = float(os.getenv("HARD_STOP_LOSS_PCT", "0.50"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5"))   # circuit breaker
# Daily dollar loss limit: once realized P&L (net of commissions) breaches -MAX_DAILY_LOSS,
# no new entries until tomorrow. Open positions are still managed by the exit rules. 0 disables.
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "400"))

# End-of-day flatten time (ET) — force-close all 0DTE positions before the 4 PM close.
_eod = os.getenv("EOD_FLATTEN_TIME", "15:55")
EOD_FLATTEN_HOUR, EOD_FLATTEN_MINUTE = (int(x) for x in _eod.split(":"))

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
