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

# ── Strategy selector ────────────────────────────────────────────────────────
# "breakout" = original VWAP+ORB+ADX debit/condor logic. "trend" = the
# Supertrend(7,3)+PSAR(0.02,0.2)+Kaufman-chop windowed strategy, SPX $10 spread,
# validated on a 3-year SPX backtest (2026-08-08, scripts/backtest_spread_dollars.py).
# Trend mode ignores the breakout entry/exit/condor/conviction/trail/VWAP-invalidation
# paths — its only exits are: hard stop, Supertrend reversal, +TP resting limit, EOD.
STRATEGY = os.getenv("STRATEGY", "breakout")
# Comma-separated → run MULTIPLE strategies at once (e.g. "trend,gex"). Each manages its
# own position per symbol (trades keyed by strategy:symbol); account-level guards are shared.
ACTIVE_STRATEGIES = [s.strip() for s in STRATEGY.split(",") if s.strip()]
# Supertrend + Kaufman params (trend mode). Entry = Supertrend flip INTO a direction
# AND PSAR agrees AND kaufman-chop <= TREND_KAUF_MAX, only inside a TREND_WINDOWS slot.
TREND_SUPERTREND_PERIOD = int(os.getenv("TREND_SUPERTREND_PERIOD", "7"))
TREND_SUPERTREND_MULT = float(os.getenv("TREND_SUPERTREND_MULT", "3.0"))
TREND_KAUF_N = int(os.getenv("TREND_KAUF_N", "14"))
TREND_KAUF_MAX = float(os.getenv("TREND_KAUF_MAX", "50"))
TREND_MIN_BARS = int(os.getenv("TREND_MIN_BARS", "20"))     # session warmup before signalling
# Entry windows (ET), comma-separated HH:MM-HH:MM. Only enter inside one of them.
# 2026-08-09: single-leg pivot → drop the power hour (theta kills naked longs late),
# trade the open + midday until 2 PM. See the single-leg backtest (+$12,985/3yr, t≈1.3).
TREND_WINDOWS = os.getenv("TREND_WINDOWS", "09:30-14:00")
# Structure: 'single' = buy one ATM (~50Δ) directional leg (CALL on up-flip, PUT on down-
# flip); 'spread' = the ATM debit vertical. Single-leg halves the fee and uncaps the winner
# (its edge is the convex tail), so single-leg mode also runs with NO take-profit.
TREND_LEGS = os.getenv("TREND_LEGS", "single")
# Skip an entry when entry-time realized vol (open→now, annualized, NO lookahead) is below
# this — a quiet-so-far day starves a naked long (it just bleeds theta). 0 disables.
TREND_SKIP_LOWIV = float(os.getenv("TREND_SKIP_LOWIV", "0.082"))

# ── GEX strategy (STRATEGY='gex') — dealer gamma-flip momentum ────────────────
# FORWARD-TEST ONLY (no historical GEX data exists for free). Entry = negative-gamma
# regime (SPX < Gflip, OR breaking out of a concentration zone) + 15-min opening-range
# breakout + short-term price momentum, inside a GEX_WINDOWS slot. Exits reuse
# HARD_STOP_LOSS_PCT (0.50) + TAKE_PROFIT_TARGET_PCT (0.60) + OR-reclaim invalidation
# + GEX_FLATTEN_TIME. GEX math lives in src/gex.py (unit-tested); the live chain (OI +
# IV per strike) is pulled from IBKR — gate: verify OI streams on the Options Add-On feed.
GEX_WINDOWS = os.getenv("GEX_WINDOWS", "09:30-15:55")                 # OR-breakout gates entries to post-9:45
GEX_OR_MINUTES = int(os.getenv("GEX_OR_MINUTES", "15"))               # opening range = 9:30 + this many min
GEX_FLATTEN_TIME = os.getenv("GEX_FLATTEN_TIME", "15:55")             # flatten all positions by 3:55 PM
# GEX is single-leg → NO take-profit by default (a TP caps the convex tail that IS the edge,
# same lesson as trend). 0 = off; set >0 (e.g. 0.60) only to re-enable a hard TP.
GEX_TAKE_PROFIT = float(os.getenv("GEX_TAKE_PROFIT", "0.0"))
# Structure: single-leg directional (2026-08-09 pivot — NO spreads anywhere). Buy one
# ~50Δ leg: CALL on a bullish GEX signal, PUT on bearish. Single-leg = half the fee +
# uncapped convex tail, which is exactly what a fast negative-gamma move pays for.
GEX_LEGS = os.getenv("GEX_LEGS", "single")
GEX_LONG_DELTA = float(os.getenv("GEX_LONG_DELTA", "0.50"))           # the ~50Δ leg we buy
# (spread params retained for optional A/B only; unused while GEX_LEGS=single)
GEX_SHORT_DELTA = float(os.getenv("GEX_SHORT_DELTA", "0.22"))
GEX_SPREAD_WIDTH = float(os.getenv("GEX_SPREAD_WIDTH", "10"))
GEX_MOMENTUM_BARS = int(os.getenv("GEX_MOMENTUM_BARS", "2"))          # "delta acceleration" ≈ price momentum over N bars
GEX_INVALIDATION_BARS = int(os.getenv("GEX_INVALIDATION_BARS", "3"))  # 3 closes back inside the OR → exit
GEX_WALL_TOL_PCT = float(os.getenv("GEX_WALL_TOL_PCT", "0.0015"))     # "at a wall" tolerance (~0.15% of spot)
GEX_CHAIN_STRIKE_PCT = float(os.getenv("GEX_CHAIN_STRIKE_PCT", "0.05"))  # fetch strikes within ±5% of spot (far OI ≈ 0 gamma)
GEX_CHAIN_EXPIRIES = int(os.getenv("GEX_CHAIN_EXPIRIES", "3"))        # nearest N expirations to include in GEX
GEX_CHAIN_MAX_STRIKES = int(os.getenv("GEX_CHAIN_MAX_STRIKES", "50")) # cap strikes nearest ATM (data-line budget)
GEX_REFRESH_MIN = int(os.getenv("GEX_REFRESH_MIN", "30"))             # re-fetch OI chain every N min (OI is ~static intraday)
# Each (symbol, direction) pair cools down for SIGNAL_COOLDOWN_MINUTES after a trade,
# then can re-trigger. MAX_TRADES_PER_DAY is the overall daily cap across all symbols.
# With 3 symbols × 2 directions and a 30-min cooldown in a 5.5-hr window: ~12 is realistic.
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "12"))
SIGNAL_COOLDOWN_MINUTES = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "30"))
# Skip a trade if the spread's mid is below this. NOT a risk knob — a FEE knob:
# fees are per-contract, so a cheap spread buys the MOST contracts and therefore
# the most fees. 2026-07-15: a $0.14 spread at a $300 budget = 21 lots = 42 legs =
# $95 in fees on an $80 loss — and only ~$81 net even if the +60% target had hit.
# Raised 0.10 → 0.30 on 2026-07-15 (TODO #35).
MIN_SPREAD_COST = float(os.getenv("MIN_SPREAD_COST", "0.30"))
# #34 (2026-07-15): an entry limit has a shelf life measured in bars, not hours.
# A resting limit only fills once the spread decays to our bid — i.e. once the
# market has moved AGAINST the thesis (adverse selection). On 07-15 an order sat
# 1h42m, filled, and invalidated 65s later. Cancel an unfilled entry after this
# many seconds and let the signal be re-evaluated fresh. 0 disables (wait forever).
ENTRY_ORDER_TIMEOUT_SECONDS = int(os.getenv("ENTRY_ORDER_TIMEOUT_SECONDS", "120"))
# 2026-08-07 (user trade analysis): entry limits at the spread MID don't fill on a
# moving/breakout tape (the market runs away from a passive bid) → orders time out,
# we miss the good early entry and chase a worse late one. Price the entry limit
# this fraction of the way from mid → ask: 0 = mid (old), 0.5 = (mid+ask)/2, 1 = ask.
# On SPX the leg spread is ~$0.10, so crossing costs pennies vs missing the trade.
ENTRY_AGGRESSION = float(os.getenv("ENTRY_AGGRESSION", "0.5"))
STRIKE_STEP = {"SPY": 1, "QQQ": 1, "IWM": 1, "XSP": 1, "SPX": 5}   # SPX 0DTE lists 5-pt strikes ATM (was 25 — too coarse; validate on the chain)
# SPX widened 5→10 for the trend strategy (2026-08-08): the fixed per-contract fee is
# a smaller drag on a $10 spread, which cleared the fee wall far better in backtest
# (+$1,328 vs +$289 over 3yr). A $10-wide SPX spread = long ATM, short ATM±10 (2 strikes).
SPREAD_WIDTH = {"SPY": 1, "QQQ": 1, "IWM": 1, "XSP": 1, "SPX": 10}

# ── Signal source vs execution symbol (#3, XSP migration) ────────────────────
# Some tradables are illiquid as an underlying but track a liquid proxy. XSP
# (cash-settled, European → no assignment) has thin option/where-volume, so its
# 1-min bars make an unreliable VWAP. We therefore compute the ENTRY/EXIT
# indicators (VWAP, ORB, ADX) from the proxy's bars (SPY — real volume) but
# select strikes and place orders on the execution symbol itself (XSP). A symbol
# absent from this map sources its own bars (SPY/QQQ/IWM unchanged).
SIGNAL_SOURCE = {"XSP": "SPY", "SPX": "SPY"}   # both index products have thin/no-volume bars → source VWAP from SPY

# ── Chop guards (added after 2026-07-01 reversal-day retro) ──────────────────
# Entry gate: require ADX to be RISING over the last N bars, not just > 25.
# A level check passes on residual momentum; the slope says the trend is alive.
# 0 disables. Fail-open when slope can't be computed yet (early session NaNs).
ADX_SLOPE_BARS = int(os.getenv("ADX_SLOPE_BARS", "10"))
# #32 (regime-aware entry): the rising-ADX gate above blocked EVERY QQQ entry on
# 2026-07-13 — QQQ's biggest down day (ADX high but not rising). Override: when
# ADX is at/above this level the trend is already strong enough to enter even if
# the slope is flat/falling. 0 disables (pure rising-ADX gate, pre-#32 behavior).
# Needs replay validation before trusting live; start conservative (e.g. 40).
ADX_SLOPE_OVERRIDE_ADX = float(os.getenv("ADX_SLOPE_OVERRIDE_ADX", "0"))
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
# ── #32 regime-aware invalidation (added after the 2026-07-13 trend-day whipsaw) ─
# On a −2% down day our PUTs were RIGHT, but a normal pullback ticked price back
# across VWAP by a few cents and the recross exit ejected us right before the move
# paid (SPY exited 751.45 → closed ~748.2). Two guards make the recross count only
# when the thesis is really dead, not on trend-day noise. Both default OFF (0 =
# pre-#32 behavior); calibrate with scripts/replay_invalidation.py before enabling.
#  (a) BUFFER: a wrong-side close only counts if it clears VWAP by this fraction
#      (0.0005 = 0.05%), not a bare tick. Symmetric to ORB_BREAKOUT_BUFFER_PCT.
VWAP_INVALIDATION_BUFFER_PCT = float(os.getenv("VWAP_INVALIDATION_BUFFER_PCT", "0"))
#  (b) TREND HOLD: suppress VWAP invalidation while a strong trend (ADX >= this)
#      runs IN THE TRADE'S FAVOR (DI+>DI− for a CALL, DI−>DI+ for a PUT) — then a
#      pullback is noise; rely on hard stop / trail. A strong trend AGAINST us still
#      invalidates (never ride an adverse trend to the −70% stop). NB: DI(14) flips
#      on a few counter-bars, so this rarely fires at N=6 — it's a safety gate, not
#      a big lever. 0 disables. Start high (e.g. 35).
VWAP_INVALIDATION_HOLD_ADX = float(os.getenv("VWAP_INVALIDATION_HOLD_ADX", "0"))
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

# #42 (2026-07-28): FADE-THE-BREAKOUT. Execute the OPPOSITE of each CALL/PUT signal
# (all filters + conviction still run on the original signal; only the traded side
# inverts). backtest_39.py showed our breakout DIRECTION is systematically wrong in
# this mean-reverting tape: BASELINE flipped went 39%→61% win, −9→+50 bp gross.
# ⚠️ REGIME BET — fading breakouts LOSES in a trending stretch; watch for a trend guard.
# ⚠️ Edge is thin (~+2 bp/trade) vs SPX fees — unproven until real fills land.
FLIP_DIRECTION = os.getenv("FLIP_DIRECTION", "false").lower() == "true"

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
