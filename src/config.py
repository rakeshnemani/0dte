import os
from dotenv import load_dotenv

load_dotenv()

# IBKR connection settings
IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "4002"))        # 4002 = IB Gateway paper, 7497 = TWS paper
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))
# Market data type: 1 = live (real-time), 2 = frozen, 3 = delayed, 4 = delayed-frozen.
# Default 1 — requires active data subscriptions (paper inherits the live account's).
# Use 3/4 only if running without subscriptions.
IBKR_MARKET_DATA_TYPE = int(os.getenv("IBKR_MARKET_DATA_TYPE", "1"))

SYMBOLS = os.getenv("SYMBOLS", "SPY,QQQ,IWM").split(",")
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "200.0"))
# Each (symbol, direction) pair cools down for SIGNAL_COOLDOWN_MINUTES after a trade,
# then can re-trigger. MAX_TRADES_PER_DAY is the overall daily cap across all symbols.
# With 3 symbols × 2 directions and a 30-min cooldown in a 5.5-hr window: ~12 is realistic.
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "12"))
SIGNAL_COOLDOWN_MINUTES = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "30"))
MIN_SPREAD_COST = float(os.getenv("MIN_SPREAD_COST", "0.10"))
STRIKE_STEP = {"SPY": 1, "QQQ": 1, "IWM": 1, "XSP": 1, "SPX": 25}
SPREAD_WIDTH = {"SPY": 1, "QQQ": 1, "IWM": 1, "XSP": 1, "SPX": 5}

# ── Chop guards (added after 2026-07-01 reversal-day retro) ──────────────────
# Entry gate: require ADX to be RISING over the last N bars, not just > 25.
# A level check passes on residual momentum; the slope says the trend is alive.
# 0 disables. Fail-open when slope can't be computed yet (early session NaNs).
ADX_SLOPE_BARS = int(os.getenv("ADX_SLOPE_BARS", "10"))
# Entry gate: price must clear the ORB level by this fraction (0.001 = 0.1%),
# not just poke a cent above it. Filters micro-poke false breakouts.
ORB_BREAKOUT_BUFFER_PCT = float(os.getenv("ORB_BREAKOUT_BUFFER_PCT", "0.001"))
# Path-aware entry (#31 — the bear-trap fix). A breakout signal must be a real
# breakout, not a hover: (1) the trigger level must have been crossed within the
# last PATH_FRESH_BARS (else the break is stale and price is just sitting/
# recovering near the line); (2) the net move of the last PATH_MOMENTUM_BARS
# closes must agree with the signal direction (never fade the last 3 bars —
# both 5/5 bear traps entered PUTs against a 3-green-bar bounce). 0 disables.
PATH_FRESH_BARS = int(os.getenv("PATH_FRESH_BARS", "10"))
PATH_MOMENTUM_BARS = int(os.getenv("PATH_MOMENTUM_BARS", "3"))

# Exit: if price closes on the wrong side of VWAP for N consecutive 1-min bars,
# the entry thesis is invalidated — exit instead of riding to the hard stop.
# 0 disables.
VWAP_INVALIDATION_BARS = int(os.getenv("VWAP_INVALIDATION_BARS", "3"))
# Entry throttle: after this many thesis-invalidation exits on the same
# (symbol, direction) in one day, stand down on that signal until tomorrow.
# 0 disables.
MAX_INVALIDATIONS_PER_SIGNAL = int(os.getenv("MAX_INVALIDATIONS_PER_SIGNAL", "2"))

# Conviction-based position sizing: each entry is scored 0-5 (ADX strength,
# ADX slope, cross-symbol agreement, open-drive timing, calm tape; minus one
# per invalidation exit already today). Budget = MAX_POSITION_SIZE × multiplier.
CONVICTION_SIZING_ENABLED = os.getenv("CONVICTION_SIZING_ENABLED", "true").lower() == "true"
CONVICTION_LOW_MULT = float(os.getenv("CONVICTION_LOW_MULT", "0.5"))    # score <= 1
CONVICTION_HIGH_MULT = float(os.getenv("CONVICTION_HIGH_MULT", "1.5"))  # score >= 4
# Minimum score to trade at all. LOW-tier record through 2026-07-08: 1W/5L,
# -$147 gross — and tiny positions can't clear the per-contract fee floor.
# Below this score the right size is zero, not half. Set to -99 to disable.
MIN_CONVICTION_SCORE = int(os.getenv("MIN_CONVICTION_SCORE", "2"))

# Take-profit target: on entry fill, a resting limit sell is parked at
# entry x (1 + this). Max peak ever recorded is +64.6% (all winners peak
# 48-65%) — waiting for +100% means holding gamma risk for value that only
# exists at expiry. Resting form fills between heartbeats and sells into
# strength. 0 disables.
TAKE_PROFIT_TARGET_PCT = float(os.getenv("TAKE_PROFIT_TARGET_PCT", "0.60"))

# ── Iron condors (regime-matched credit side) ────────────────────────────────
# On proven range days (low ADX + many VWAP crosses — the regime where the
# debit playbook structurally loses), sell an iron condor around the day's
# range instead: short strikes just outside the day high/low, wings
# SPREAD_WIDTH out. Entered 11:00–13:30 ET only. Sized by max loss
# ((width − credit) × 100 × qty <= MAX_POSITION_SIZE).
CONDOR_ENABLED = os.getenv("CONDOR_ENABLED", "true").lower() == "true"
CONDOR_MAX_ADX = float(os.getenv("CONDOR_MAX_ADX", "22"))            # trend must be absent
CONDOR_MIN_VWAP_CROSSES = int(os.getenv("CONDOR_MIN_VWAP_CROSSES", "8"))  # chop must be proven
MIN_CONDOR_CREDIT = float(os.getenv("MIN_CONDOR_CREDIT", "0.15"))    # skip if premium too thin
CONDOR_TP_PCT = float(os.getenv("CONDOR_TP_PCT", "0.50"))            # buy back at 50% of credit

# Fast exit polling: the main loop runs every 60s normally, but drops to
# FAST_POLL_SECONDS when an exit needs tight watching — a closing order in
# flight, or an ACTIVE trade with profit >= FAST_POLL_ARM_PCT (approaching the
# trail trigger). Fixes the sampling slippage where fast moves blew 10-16 pts
# past exit thresholds between 60s checks (2026-07-07 QQQ). 0 disables.
FAST_POLL_SECONDS = int(os.getenv("FAST_POLL_SECONDS", "15"))
FAST_POLL_ARM_PCT = float(os.getenv("FAST_POLL_ARM_PCT", "0.35"))

# Trailing stop activates only once a trade has peaked at TAKE_PROFIT_TRAIL_TRIGGER.
# Below that the position rides untouched (only the hard stop applies). Once armed,
# it exits if profit falls to (1 - TRAILING_STOP_LOSS_PCT) of the peak — i.e. gives
# back 10% OF the peak (peak +50% -> exit +45%).
TAKE_PROFIT_TRAIL_TRIGGER = float(os.getenv("TAKE_PROFIT_TRAIL_TRIGGER", "0.50"))
TRAILING_STOP_LOSS_PCT = float(os.getenv("TRAILING_STOP_LOSS_PCT", "0.10"))
HARD_STOP_LOSS_PCT = float(os.getenv("HARD_STOP_LOSS_PCT", "0.70"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5"))
# Daily dollar loss limit: once realized P&L (net of commissions) for the day
# breaches -MAX_DAILY_LOSS, no new entries until tomorrow. Open positions are
# still managed by the exit rules. 0 disables.
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "400"))

# End-of-day flatten time (ET) — force-close all 0DTE positions before the 4 PM close.
_eod = os.getenv("EOD_FLATTEN_TIME", "15:55")
EOD_FLATTEN_HOUR, EOD_FLATTEN_MINUTE = (int(x) for x in _eod.split(":"))

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
