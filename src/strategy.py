"""Pure strategy logic — indicators, entry signal, conviction score, exit rules.

Everything here is a function of dataframes/values in → decisions out. No IBKR
calls, no Discord, no file I/O — which keeps the trading rules readable and
independently testable. Orchestration lives in bot.py; market data in broker.py.
"""
import datetime
import logging
import math
from typing import Optional, Tuple

import pandas as pd
import ta

import config

logger = logging.getLogger(__name__)

# Conviction-score thresholds (see conviction_score)
CONV_ADX_MIN = 30.0          # ADX level considered "strong"
CONV_SLOPE_MIN = 3.0         # ADX slope considered "steeply rising"
CONV_MAX_VWAP_CROSSES = 4    # more crosses than this = choppy tape
CONV_EARLY_HOUR = 11         # entries before this ET hour are "open drive"


# ── Indicators ───────────────────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add VWAP and ADX(14) columns to a 1-min bar dataframe (in place)."""
    vwap = ta.volume.VolumeWeightedAveragePrice(
        high=df['high'], low=df['low'], close=df['close'], volume=df['volume']
    )
    df.loc[:, 'VWAP'] = vwap.volume_weighted_average_price()
    adx = ta.trend.ADXIndicator(
        high=df['high'], low=df['low'], close=df['close'], window=14
    )
    df.loc[:, 'ADX'] = adx.adx()
    return df


def orb_levels(df: pd.DataFrame, now: datetime.datetime) -> Optional[Tuple[float, float]]:
    """High/low of the 9:30–10:00 ET opening range, anchored to wall-clock time
    (not df.index[0]) so mid-day restarts still produce the correct range."""
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    orb_end = market_open + datetime.timedelta(minutes=30)
    orb_bars = df[(df.index >= market_open) & (df.index < orb_end)]
    if orb_bars.empty:
        return None
    return float(orb_bars['high'].max()), float(orb_bars['low'].min())


# ── Entry signal ─────────────────────────────────────────────────────────────

def entry_signal(symbol: str, df: pd.DataFrame, now: datetime.datetime
                 ) -> Tuple[Optional[str], str, dict, Optional[str]]:
    """Evaluate VWAP + 30-min ORB + ADX for an entry signal.

    Returns (direction, reason, indicators, lean) where direction is
    'CALL'/'PUT'/None and lean is the raw price-vs-VWAP direction (recorded by
    the bot for cross-symbol agreement even when no signal fires).
    Expects df to already contain enough bars (caller guards length).
    """
    try:
        add_indicators(df)

        orb = orb_levels(df, now)
        if orb is None:
            return None, "", {}, None
        orb_high, orb_low = orb

        current_close = df['close'].iloc[-1]
        current_vwap = df['VWAP'].iloc[-1]
        current_adx = df['ADX'].iloc[-1]

        # Raw directional lean — used by other symbols' conviction scores
        lean = 'CALL' if current_close > current_vwap else 'PUT'

        if pd.isna(current_adx) or current_adx < 25:
            return None, "", {}, lean

        # ── Chop guard 1: ADX must be RISING, not just above 25 ───────────────
        # 2026-07-01: ADX direction predicted all 5 outcomes — every hard-stop
        # loser entered on flat/fading ADX that then collapsed. Fails open early
        # in the session when the lookback bar is still NaN.
        adx_slope = None
        if config.ADX_SLOPE_BARS > 0 and len(df) > config.ADX_SLOPE_BARS:
            adx_prev = df['ADX'].iloc[-1 - config.ADX_SLOPE_BARS]
            if not pd.isna(adx_prev):
                adx_slope = float(current_adx - adx_prev)
                if adx_slope <= 0:
                    # #32 override: a strong-enough trend can enter even on a flat/
                    # falling slope. The pure rising-ADX gate blocked EVERY QQQ entry
                    # on 2026-07-13, QQQ's biggest down day (ADX high, not rising).
                    override = config.ADX_SLOPE_OVERRIDE_ADX
                    if override > 0 and current_adx >= override:
                        logger.info(
                            f"[{symbol}] ADX slope {adx_slope:+.2f} flat/falling but ADX "
                            f"{current_adx:.1f} >= override {override:.0f} — entry allowed (#32)."
                        )
                    else:
                        logger.info(
                            f"[{symbol}] Chop guard: ADX {current_adx:.1f} but flat/falling "
                            f"({adx_slope:+.2f} over {config.ADX_SLOPE_BARS} bars). Entry blocked."
                        )
                        return None, "", {}, lean

        indicators = {
            'adx': current_adx, 'vwap': current_vwap,
            'orb_high': orb_high, 'orb_low': orb_low, 'current_price': current_close,
            'adx_slope': adx_slope,
        }

        # Slope annotation — honest about direction (an override entry has slope ≤ 0,
        # so don't call it "rising"; the #32 override deliberately admits those).
        if adx_slope is None:
            slope_txt = ""
        elif adx_slope > 0:
            slope_txt = f" rising {adx_slope:+.2f}"
        else:
            slope_txt = f" flat/falling {adx_slope:+.2f}"

        # ── Chop guard 2: breakout must CLEAR the ORB level, not poke it ──────
        buf = config.ORB_BREAKOUT_BUFFER_PCT
        call_level = orb_high * (1 + buf)
        put_level = orb_low * (1 - buf)

        if current_close > current_vwap and current_close > call_level:
            direction = "CALL"
            level = call_level
            reason = (
                f"Bullish: Price ({current_close:.2f}) > VWAP ({current_vwap:.2f}) and "
                f"ORB High+buffer ({call_level:.2f}). ADX: {current_adx:.2f}" + slope_txt
            )
        elif current_close < current_vwap and current_close < put_level:
            direction = "PUT"
            level = put_level
            reason = (
                f"Bearish: Price ({current_close:.2f}) < VWAP ({current_vwap:.2f}) and "
                f"ORB Low−buffer ({put_level:.2f}). ADX: {current_adx:.2f}" + slope_txt
            )
        else:
            return None, "", {}, lean

        # ── Chop guard 3 (#31): path-aware entry — the bear-trap fix ──────────
        ok, path_detail = path_confirms(direction, df, level)
        if not ok:
            logger.info(f"[{symbol}] Path guard blocked {direction}: {path_detail}")
            return None, "", {}, lean
        reason += f" | path✓ ({path_detail})"

        return direction, reason, indicators, lean

    except Exception as e:
        logger.error(f"Error calculating indicators for {symbol}: {e}")
        return None, "", {}, None


def path_confirms(direction: str, df: pd.DataFrame, level: float) -> Tuple[bool, str]:
    """#31 — path-aware entry check. A breakout must be a real breakout:

    1. FRESH: the trigger level was crossed within the last PATH_FRESH_BARS —
       i.e. at least one recent close sat on the OTHER side of the level. If
       every recent close is already beyond it, the break is stale and price is
       hovering/recovering at the line (`price < level` alone can't tell a fresh
       breakdown from a bounce ticking back under it — the 07-09/07-10 traps).
    2. MOMENTUM: the net move of the last PATH_MOMENTUM_BARS closes must agree
       with the signal — never fade the last 3 bars. Both 5/5 bear traps entered
       PUTs against a 3-green-bar bounce off the morning low.

    Pure function; returns (ok, detail). Fails open if disabled or data-short.
    """
    closes = df['close']

    n = config.PATH_FRESH_BARS
    if n > 0 and len(closes) > n:
        prior = closes.iloc[-1 - n:-1]
        if direction == 'CALL':
            fresh = bool((prior <= level).any())   # recently came from BELOW the level
        else:
            fresh = bool((prior >= level).any())   # recently came from ABOVE the level
        if not fresh:
            return False, f"level {level:.2f} not crossed in last {n} bars — stale break / hovering"

    m = config.PATH_MOMENTUM_BARS
    if m > 0 and len(closes) > m:
        net = float(closes.iloc[-1] - closes.iloc[-1 - m])
        if direction == 'CALL' and net <= 0:
            return False, f"last {m} bars net {net:+.2f} (falling) against a CALL — fading a pullback"
        if direction == 'PUT' and net >= 0:
            return False, f"last {m} bars net {net:+.2f} (rising) against a PUT — fading a bounce"
        return True, f"fresh break, {m}-bar net {net:+.2f} with signal"

    return True, "path checks disabled/insufficient bars"


# ── Iron condor (regime-matched credit side) ─────────────────────────────────

# Entry window: after the range has proven itself, while premium remains
CONDOR_WINDOW_START = (11, 0)    # ET
CONDOR_WINDOW_END = (13, 30)
# Range thesis is dead when price closes beyond a short strike this many bars
CONDOR_BREACH_BARS = 2


def condor_signal(symbol: str, df: pd.DataFrame, now: datetime.datetime,
                  strike_step: float, wing_width: float
                  ) -> Tuple[bool, str, dict]:
    """Range-day signal: sell an iron condor around the day's proven range.

    Fires only in the regime where the debit playbook structurally loses —
    no trend (ADX < CONDOR_MAX_ADX) plus proven chop (>= CONDOR_MIN_VWAP_CROSSES
    VWAP crosses), inside the 11:00–13:30 ET window. Short strikes sit just
    outside the day high/low; wings are wing_width further out.
    Returns (ok, reason, indicators) — indicators carries the four strikes.
    """
    hm = (now.hour, now.minute)
    if hm < CONDOR_WINDOW_START or hm > CONDOR_WINDOW_END:
        return False, "", {}
    if df.empty or len(df) < 60:
        return False, "", {}

    try:
        if 'ADX' not in df.columns or 'VWAP' not in df.columns:
            add_indicators(df)

        current_adx = df['ADX'].iloc[-1]
        if pd.isna(current_adx) or current_adx >= config.CONDOR_MAX_ADX:
            return False, "", {}

        above = (df['close'] > df['VWAP']).dropna()
        crosses = int((above != above.shift()).sum()) - 1 if len(above) > 1 else 0
        if crosses < config.CONDOR_MIN_VWAP_CROSSES:
            return False, "", {}

        day_high = float(df['high'].max())
        day_low = float(df['low'].min())
        current = float(df['close'].iloc[-1])

        # Short strikes just OUTSIDE the day's range; wings one width further
        short_call = math.ceil((day_high + 1e-9) / strike_step) * strike_step
        short_put = math.floor((day_low - 1e-9) / strike_step) * strike_step
        if short_call - short_put < 2 * strike_step:      # range too narrow
            return False, "", {}
        if not (short_put < current < short_call):        # already at an edge
            return False, "", {}

        indicators = {
            'adx': float(current_adx), 'vwap': float(df['VWAP'].iloc[-1]),
            'current_price': current, 'vwap_crosses': crosses,
            'orb_high': day_high, 'orb_low': day_low,   # audit columns reuse
            'short_call': short_call, 'wing_call': short_call + wing_width,
            'short_put': short_put, 'wing_put': short_put - wing_width,
        }
        reason = (
            f"Range day: ADX {current_adx:.1f} < {config.CONDOR_MAX_ADX:.0f}, "
            f"{crosses} VWAP crosses, range {day_low:.2f}–{day_high:.2f}. "
            f"Selling {short_put:.0f}/{short_call:.0f} condor (wings ±{wing_width:.0f})."
        )
        return True, reason, indicators
    except Exception as e:
        logger.error(f"Error evaluating condor signal for {symbol}: {e}")
        return False, "", {}


def condor_breached(df: pd.DataFrame, short_call: float, short_put: float) -> bool:
    """Range thesis invalidated: price closed beyond a short strike for
    CONDOR_BREACH_BARS consecutive 1-min bars."""
    n = CONDOR_BREACH_BARS
    if df.empty or len(df) < n:
        return False
    closes = df['close'].tail(n)
    return bool((closes > short_call).all() or (closes < short_put).all())


# ── Breadth annotation ───────────────────────────────────────────────────────

def breadth_confirms(direction: str, tick_df: pd.DataFrame, vold_df: pd.DataFrame
                     ) -> Tuple[bool, str]:
    """Check whether NYSE breadth (TICK + VOLD) confirms the trade direction.
    Logged as an annotation, not used as a gate. Fails open on missing data.

    CALL: VOLD slope > 0 AND TICK higher lows >= 60% of bars AND avg TICK > -200
    PUT:  VOLD slope < 0 AND TICK NOT making higher lows AND avg TICK < 200
    """
    N = 10  # look back 10 bars (~10 minutes)
    if tick_df.empty or vold_df.empty or len(tick_df) < N or len(vold_df) < N:
        return True, "breadth data insufficient — filter bypassed"

    tick_close = tick_df['close'].tail(N)
    tick_lows = tick_df['low'].tail(N) if 'low' in tick_df.columns else tick_close
    vold_close = vold_df['close'].tail(N)

    avg_tick = float(tick_close.mean())
    vold_slope = float(vold_close.iloc[-1] - vold_close.iloc[0])

    lows = tick_lows.values
    higher_lows = sum(1 for i in range(1, len(lows)) if lows[i] >= lows[i - 1])
    tick_hl = higher_lows >= (len(lows) - 1) * 0.6   # >=60% of consecutive pairs

    if direction == 'CALL':
        confirmed = (vold_slope > 0) and tick_hl and (avg_tick > -200)
    else:
        confirmed = (vold_slope < 0) and (not tick_hl) and (avg_tick < 200)

    reason = (
        f"VOLD slope {vold_slope:+.0f} | TICK avg {avg_tick:+.0f} | "
        f"Higher lows {'✓' if tick_hl else '✗'} ({higher_lows}/{len(lows)-1})"
    )
    return confirmed, reason


# ── Conviction score ─────────────────────────────────────────────────────────

def conviction_score(symbol: str, direction: str, df: pd.DataFrame,
                     indicators: dict, now: datetime.datetime,
                     symbol_lean: dict, invalidation_total: int) -> dict:
    """Score entry conviction 0-5; the score sizes the position.

    +1 each: ADX >= 30, ADX slope >= +3, >=1 other symbol leaning the same
    direction, entry before 11:00 ET, calm tape (<= 4 VWAP crosses today).
    -1 per thesis-invalidation exit already taken today (any signal).
    Tier: <= 1 LOW (0.5x budget), 2-3 MEDIUM (1x), >= 4 HIGH (1.5x).
    """
    score, parts = 0, []

    if indicators['adx'] >= CONV_ADX_MIN:
        score += 1
        parts.append("ADX✓")
    else:
        parts.append("ADX✗")

    slope = indicators.get('adx_slope')
    if slope is not None and slope >= CONV_SLOPE_MIN:
        score += 1
        parts.append("slope✓")
    else:
        parts.append("slope✗")

    agree = [s for s, (lean, ts) in symbol_lean.items()
             if s != symbol and lean == direction
             and (now - ts).total_seconds() < 300]
    if agree:
        score += 1
        parts.append(f"agree✓({'+'.join(agree)})")
    else:
        parts.append("agree✗")

    if now.hour < CONV_EARLY_HOUR:
        score += 1
        parts.append("early✓")
    else:
        parts.append("early✗")

    above = (df['close'] > df['VWAP']).dropna()
    crosses = int((above != above.shift()).sum()) - 1 if len(above) > 1 else 0
    if crosses <= CONV_MAX_VWAP_CROSSES:
        score += 1
        parts.append(f"tape✓({crosses}x)")
    else:
        parts.append(f"tape✗({crosses}x)")

    if invalidation_total:
        score -= invalidation_total
        parts.append(f"inv−{invalidation_total}")

    if score <= 1:
        tier, mult = 'LOW', config.CONVICTION_LOW_MULT
    elif score >= 4:
        tier, mult = 'HIGH', config.CONVICTION_HIGH_MULT
    else:
        tier, mult = 'MEDIUM', 1.0

    detail = ' '.join(parts)
    return {'score': score, 'tier': tier, 'mult': mult, 'detail': detail,
            'str': f"{tier} {score}/5 | {detail}"}


# ── Exit rules ───────────────────────────────────────────────────────────────

def thesis_invalidated(direction: str, df: pd.DataFrame) -> bool:
    """True if price closed on the wrong side of VWAP for
    VWAP_INVALIDATION_BARS consecutive 1-min bars — the entry reason is gone.

    #32 regime-awareness (both default-off → identical to the pre-#32 rule):
      • TREND HOLD — while a strong trend (ADX >= VWAP_INVALIDATION_HOLD_ADX) is
        running IN THE TRADE'S FAVOR (DI+ > DI− for a CALL, DI− > DI+ for a PUT),
        a VWAP pullback is trend noise, not a dead thesis; suppress the recross
        exit (hard stop / trail still apply). Direction matters: a strong trend
        AGAINST us must still invalidate — otherwise we'd ride an adverse trend to
        the −70% stop (the 07-01 bleed). Fails open on any ADX error.
      • BUFFER — a wrong-side close must clear VWAP by VWAP_INVALIDATION_BUFFER_PCT,
        not a bare tick (entries are born just beyond VWAP; a few-cent recross is
        usually noise — the 2026-07-13 SPY whipsaw: 751.45 vs VWAP 751.41).
    """
    n = config.VWAP_INVALIDATION_BARS
    if n <= 0 or df.empty or len(df) < max(20, n):
        return False

    hold_adx = config.VWAP_INVALIDATION_HOLD_ADX
    if hold_adx > 0:
        try:
            adx_ind = ta.trend.ADXIndicator(
                high=df['high'], low=df['low'], close=df['close'], window=14
            )
            adx = adx_ind.adx().iloc[-1]
            di_pos = adx_ind.adx_pos().iloc[-1]
            di_neg = adx_ind.adx_neg().iloc[-1]
            if not (pd.isna(adx) or pd.isna(di_pos) or pd.isna(di_neg)) and adx >= hold_adx:
                trend_favors = (di_pos > di_neg) if direction == 'CALL' else (di_neg > di_pos)
                if trend_favors:            # strong trend WITH us → pullback is noise
                    return False
        except Exception:   # never let an indicator hiccup mask a real exit
            pass

    vwap_series = ta.volume.VolumeWeightedAveragePrice(
        high=df['high'], low=df['low'], close=df['close'], volume=df['volume']
    ).volume_weighted_average_price()
    closes = df['close'].tail(n)
    vwaps = vwap_series.tail(n)
    buf = config.VWAP_INVALIDATION_BUFFER_PCT
    if direction == 'CALL':
        return bool((closes < vwaps * (1 - buf)).all())
    return bool((closes > vwaps * (1 + buf)).all())


def exit_decision(profit_pct: float, max_profit_pct: float,
                  invalidated: bool,
                  invalidation_reason: Optional[str] = None) -> Tuple[bool, str]:
    """Three-rule exit model, evaluated in priority order:
      1. Hard stop — cap the downside at HARD_STOP_LOSS_PCT.
      2. Thesis invalidation — sustained VWAP recross against the trade.
      3. Trailing stop — arms after TAKE_PROFIT_TRAIL_TRIGGER peak; exits at
         (1 - TRAILING_STOP_LOSS_PCT) of the peak. Because it only arms at
         +50%, its threshold is always >= +45% — it never closes at a loss.
    Returns (exit_triggered, reason)."""
    if profit_pct <= -config.HARD_STOP_LOSS_PCT:
        return True, f"Hard stop loss: spread lost {abs(profit_pct)*100:.1f}% of entry value"

    if invalidated:
        base = invalidation_reason or (
            f"Thesis invalidated: price closed on the wrong side of VWAP for "
            f"{config.VWAP_INVALIDATION_BARS} consecutive bars"
        )
        return True, f"{base} (Current P&L: {profit_pct*100:+.2f}%)"

    if max_profit_pct >= config.TAKE_PROFIT_TRAIL_TRIGGER:
        trailing_threshold = max_profit_pct * (1 - config.TRAILING_STOP_LOSS_PCT)
        if profit_pct <= trailing_threshold:
            return True, (
                f"Trailing stop after +{config.TAKE_PROFIT_TRAIL_TRIGGER*100:.0f}% peak: "
                f"gave back to {profit_pct*100:.2f}% "
                f"(Peak: {max_profit_pct*100:.2f}%, Threshold: {trailing_threshold*100:.2f}%)"
            )

    return False, ""
