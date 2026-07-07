"""Pure strategy logic — indicators, entry signal, conviction score, exit rules.

Everything here is a function of dataframes/values in → decisions out. No IBKR
calls, no Discord, no file I/O — which keeps the trading rules readable and
independently testable. Orchestration lives in bot.py; market data in broker.py.
"""
import datetime
import logging
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

        # ── Chop guard 2: breakout must CLEAR the ORB level, not poke it ──────
        buf = config.ORB_BREAKOUT_BUFFER_PCT
        call_level = orb_high * (1 + buf)
        put_level = orb_low * (1 - buf)

        if current_close > current_vwap and current_close > call_level:
            direction = "CALL"
            reason = (
                f"Bullish: Price ({current_close:.2f}) > VWAP ({current_vwap:.2f}) and "
                f"ORB High+buffer ({call_level:.2f}). ADX: {current_adx:.2f}"
                + (f" rising {adx_slope:+.2f}" if adx_slope is not None else "")
            )
        elif current_close < current_vwap and current_close < put_level:
            direction = "PUT"
            reason = (
                f"Bearish: Price ({current_close:.2f}) < VWAP ({current_vwap:.2f}) and "
                f"ORB Low−buffer ({put_level:.2f}). ADX: {current_adx:.2f}"
                + (f" rising {adx_slope:+.2f}" if adx_slope is not None else "")
            )
        else:
            return None, "", {}, lean

        return direction, reason, indicators, lean

    except Exception as e:
        logger.error(f"Error calculating indicators for {symbol}: {e}")
        return None, "", {}, None


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
    VWAP_INVALIDATION_BARS consecutive 1-min bars — the entry reason is gone."""
    n = config.VWAP_INVALIDATION_BARS
    if n <= 0 or df.empty or len(df) < max(20, n):
        return False
    vwap_series = ta.volume.VolumeWeightedAveragePrice(
        high=df['high'], low=df['low'], close=df['close'], volume=df['volume']
    ).volume_weighted_average_price()
    closes = df['close'].tail(n)
    vwaps = vwap_series.tail(n)
    if direction == 'CALL':
        return bool((closes < vwaps).all())
    return bool((closes > vwaps).all())


def exit_decision(profit_pct: float, max_profit_pct: float,
                  invalidated: bool) -> Tuple[bool, str]:
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
        return True, (
            f"Thesis invalidated: price closed on the wrong side of VWAP for "
            f"{config.VWAP_INVALIDATION_BARS} consecutive bars (Current P&L: {profit_pct*100:+.2f}%)"
        )

    if max_profit_pct >= config.TAKE_PROFIT_TRAIL_TRIGGER:
        trailing_threshold = max_profit_pct * (1 - config.TRAILING_STOP_LOSS_PCT)
        if profit_pct <= trailing_threshold:
            return True, (
                f"Trailing stop after +{config.TAKE_PROFIT_TRAIL_TRIGGER*100:.0f}% peak: "
                f"gave back to {profit_pct*100:.2f}% "
                f"(Peak: {max_profit_pct*100:.2f}%, Threshold: {trailing_threshold*100:.2f}%)"
            )

    return False, ""
