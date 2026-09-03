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
# ATM (~50Δ) option — CALL bullish, PUT bearish. Single leg only.
STRATEGY = os.getenv("STRATEGY", "trend,gex")
ACTIVE_STRATEGIES = [s.strip() for s in STRATEGY.split(",") if s.strip()]

# ── Trend strategy (STRATEGY='trend') ────────────────────────────────────────
# Entry = Supertrend flip INTO a direction AND PSAR agrees AND kaufman-chop <= TREND_KAUF_MAX,
# only inside a TREND_WINDOWS slot. Exits: hard stop (HARD_STOP_LOSS_PCT) + Supertrend
# reversal + EOD flatten. Built on a 3-year SPX backtest (scripts/backtest_dollars.py).
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
# trailing stop (arms once peaked at GEX_TRAIL_TRIGGER, exits on giving back a TIERED fraction
# of the peak — see GEX_TRAIL_GIVEBACK_LOW/MID/HIGH below) + a WIDE catastrophe backstop so a trade that never peaks can't ride to a
# full-premium loss. GEX_TAKE_PROFIT>0 re-enables a hard TP (off by default — the tail is the edge).
GEX_TAKE_PROFIT = float(os.getenv("GEX_TAKE_PROFIT", "0.0"))
# 2026-08-24: lowered 0.50 → 0.35. Two trades (08-19 +28%, 08-24 +40%) peaked BELOW the old
# +50% arm and gave the whole modest peak back to the −80% catastrophe stop. The arm only gates
# WHETHER the trail is active (the exit is always peak×(1−giveback)), so lowering it adds
# protection for the +35–50% peakers WITHOUT changing the big-winner exits (+79/+90/+100% all
# still trail from their own peak). Asymmetric: protects losers, doesn't cost winners. (TODO #38)
GEX_TRAIL_TRIGGER = float(os.getenv("GEX_TRAIL_TRIGGER", "0.35"))         # arm once peaked +35%
# TIERED giveback (2026-08-27): the giveback SHRINKS as the peak grows — a loose leash on a small
# gain (don't choke a nascent runner), tightening once it's a real winner. Replaces the old flat
# 0.20, which cut runners on trend days (08-27 both CALLs peaked +36/44% and trailed out +28/34%
# while SPX ran +30pts more). Bands by peak (exit = peak × (1 − giveback), still armed at 0.35):
#   [35%,50%) → give back 60% (floor peak×0.40)   ·   [50%,70%) → 35% (peak×0.65)   ·   70%+ → 20% (peak×0.80)
# The floor still ratchets UP monotonically as the peak climbs; catastrophe + EOD unchanged.
GEX_TRAIL_BAND_MID     = float(os.getenv("GEX_TRAIL_BAND_MID",     "0.50"))   # low→mid band edge
GEX_TRAIL_BAND_HIGH    = float(os.getenv("GEX_TRAIL_BAND_HIGH",    "0.70"))   # mid→high band edge
GEX_TRAIL_GIVEBACK_LOW  = float(os.getenv("GEX_TRAIL_GIVEBACK_LOW",  "0.60")) # peak in [arm, MID)
GEX_TRAIL_GIVEBACK_MID  = float(os.getenv("GEX_TRAIL_GIVEBACK_MID",  "0.35")) # peak in [MID, HIGH)
GEX_TRAIL_GIVEBACK_HIGH = float(os.getenv("GEX_TRAIL_GIVEBACK_HIGH", "0.20")) # peak >= HIGH
# Catastrophe backstop (the only downside floor for a trade that never arms the trail). Lowered
# 0.80 → 0.60 (user, 2026-09-02, "trying what I'd go live with"). On the 16-trade sample the winners
# all bottomed shallower than −53% MAE and the losers past −67%, a clean gap — so −60% caps every
# loser while sparing every winner (best backtest P&L of any level; −50%/−35% cut the 08-28 +$1,600
# winner). n small — watch for a winner that dips past −60% before recovering. 0 = no stop at all.
GEX_CATASTROPHE_STOP = float(os.getenv("GEX_CATASTROPHE_STOP", "0.60"))
# Theta protection (2026-08-11): skip a GEX entry when entry-time realized vol (open→now,
# annualized, NO lookahead) < this — a slow tape can't move fast enough for a naked leg to
# outrun theta. 0 disables.
GEX_SKIP_LOWIV = float(os.getenv("GEX_SKIP_LOWIV", "0.082"))
# Exhaustion gate (2026-08-31): skip a mechanical-GEX entry once the day has already realized
# >= this fraction of its IV-expected move (bar range ÷ expected move; see bot._entry_exhaustion).
# It's a >= test — at/above this, don't enter (the move is largely spent → chop ahead). 0 disables.
# Mechanical GEX only; thesis is human-authorised. Range_Exp_Ratio is still logged at every entry.
GEX_RANGE_EXP_MAX = float(os.getenv("GEX_RANGE_EXP_MAX", "0.8"))
GEX_MOMENTUM_BARS = int(os.getenv("GEX_MOMENTUM_BARS", "2"))          # price momentum over N bars ("delta acceleration")
GEX_WALL_TOL_PCT = float(os.getenv("GEX_WALL_TOL_PCT", "0.0015"))     # "at a wall" tolerance (~0.15% of spot)
GEX_CHAIN_STRIKE_PCT = float(os.getenv("GEX_CHAIN_STRIKE_PCT", "0.05"))  # fetch strikes within ±5% of spot
GEX_CHAIN_EXPIRIES = int(os.getenv("GEX_CHAIN_EXPIRIES", "3"))        # nearest N expirations to include
GEX_CHAIN_MAX_STRIKES = int(os.getenv("GEX_CHAIN_MAX_STRIKES", "50")) # cap strikes nearest ATM (data-line budget)
GEX_REFRESH_MIN = int(os.getenv("GEX_REFRESH_MIN", "30"))            # re-fetch OI chain every N min (OI is ~static intraday)

# ── Thesis-GEX command rail (TODO #44) ───────────────────────────────────────
# A human-in-the-loop channel: an approved thesis is dropped as a JSON command file in
# THESIS_COMMAND_DIR; the bot watches the trigger and executes a single-leg SPX option under
# 'thesis:SPX'. Exits default to the SAME convex-tail rules as GEX (trailing arm/giveback +
# catastrophe backstop + EOD flatten); the user can also close early via a `close`/`close_if`
# command. The mechanical trend+gex scanners keep running unchanged in parallel. Claude is the
# analyst/translator, the bot is the executor, the user authorises the arm. See src/commands.py.
THESIS_ENABLED = os.getenv("THESIS_ENABLED", "true").lower() in ("1", "true", "yes")
THESIS_COMMAND_DIR = os.getenv("THESIS_COMMAND_DIR", "data/commands")   # relative → resolved to repo root

# ── Shared entry/order settings ──────────────────────────────────────────────
# Each (symbol, direction) cools down for SIGNAL_COOLDOWN_MINUTES after a trade, then can
# re-trigger. MAX_TRADES_PER_DAY is the overall daily cap across all symbols/strategies.
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "12"))
SIGNAL_COOLDOWN_MINUTES = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "30"))
# Skip a trade if the option mid is below this (a FEE floor, not a risk knob — a cheap
# option buys the most contracts and therefore the most per-contract fees).
MIN_OPTION_COST = float(os.getenv("MIN_OPTION_COST", "0.30"))
# #34: an entry limit has a shelf life measured in bars, not hours — a resting limit only
# fills once the option decays to our bid (the market moved AGAINST the thesis). Cancel an
# unfilled entry after this many seconds and re-evaluate fresh. 0 disables (wait forever).
ENTRY_ORDER_TIMEOUT_SECONDS = int(os.getenv("ENTRY_ORDER_TIMEOUT_SECONDS", "120"))
# Price the entry limit this fraction of the way from mid → ask: 0 = mid, 0.5 = (mid+ask)/2,
# 1 = ask. A passive mid bid doesn't fill on a moving tape; crossing a little buys the fill.
ENTRY_AGGRESSION = float(os.getenv("ENTRY_AGGRESSION", "0.5"))
STRIKE_STEP = {"SPY": 1, "QQQ": 1, "IWM": 1, "XSP": 1, "SPX": 5}   # SPX 0DTE lists 5-pt strikes ATM

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
