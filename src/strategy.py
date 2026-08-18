"""Pure strategy logic — indicators, entry signal, conviction score, exit rules.

Everything here is a function of dataframes/values in → decisions out. No IBKR
calls, no Discord, no file I/O — which keeps the trading rules readable and
independently testable. Orchestration lives in bot.py; market data in broker.py.
"""
import datetime
import logging
import math
from typing import Optional, Tuple

import numpy as np
import pandas as pd
import ta

import config

logger = logging.getLogger(__name__)

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


# ── Trend strategy (Supertrend + PSAR + Kaufman) ─────────────────────────────
# Pure translations of scripts/backtest_dollars.py, so the live bot places
# the same trades we backtested. Indicators use only high/low/close → they run on
# SPX index bars directly (no volume needed). Used only when STRATEGY == "trend".

def supertrend_dir(df: pd.DataFrame, period: int = 7, mult: float = 3.0):
    """+1 uptrend / −1 downtrend / 0 (warmup) per bar — standard Supertrend(period, mult)."""
    hl2 = (df['high'].values + df['low'].values) / 2.0
    atr = ta.volatility.AverageTrueRange(
        df['high'], df['low'], df['close'], window=period).average_true_range().values
    close = df['close'].values
    n = len(df)
    ub, lb = hl2 + mult * atr, hl2 - mult * atr
    fub = np.zeros(n); flb = np.zeros(n); st = np.zeros(n); d = np.zeros(n, dtype=int)
    for i in range(n):
        if i == 0 or np.isnan(atr[i]):
            fub[i], flb[i], st[i], d[i] = ub[i], lb[i], ub[i], 0
            continue
        fub[i] = ub[i] if (ub[i] < fub[i-1] or close[i-1] > fub[i-1]) else fub[i-1]
        flb[i] = lb[i] if (lb[i] > flb[i-1] or close[i-1] < flb[i-1]) else flb[i-1]
        st[i] = (flb[i] if close[i] > fub[i] else fub[i]) if st[i-1] == fub[i-1] \
                else (fub[i] if close[i] < flb[i] else flb[i])
        d[i] = 1 if st[i] == flb[i] else -1
    return d


def psar_dir(df: pd.DataFrame):
    """+1 when price is ABOVE the Parabolic SAR (bullish), −1 below. Params (0.02, 0.2)."""
    ind = ta.trend.PSARIndicator(df['high'], df['low'], df['close'], step=0.02, max_step=0.2)
    return np.where(ind.psar_up().notna().values, 1, -1)


def kaufman_chop(df: pd.DataFrame, n: int = 14):
    """Kaufman chop factor = 100·(1 − Efficiency Ratio). 0 = clean trend, 100 = pure chop.
    ER = |net move over n bars| / Σ|bar-to-bar move|. LOW ⇒ trending (gate: kauf <= max)."""
    close = df['close']
    net = close.diff(n).abs()
    path = close.diff().abs().rolling(n).sum()
    er = (net / path.replace(0, np.nan)).clip(lower=0, upper=1)
    return (100 * (1 - er)).values


def trend_entry_signal(symbol: str, df: pd.DataFrame, now: datetime.datetime
                       ) -> Tuple[Optional[str], str, dict, Optional[str]]:
    """Trend entry: Supertrend FLIPS into a direction on the latest bar AND PSAR
    agrees AND kaufman-chop <= TREND_KAUF_MAX. Returns (direction, reason, indicators,
    lean). Pure — the bot gates the time window, cooldown, and risk guards.
    """
    try:
        need = max(config.TREND_MIN_BARS, config.TREND_KAUF_N + 2,
                   config.TREND_SUPERTREND_PERIOD + 2)
        if df.empty or len(df) < need:
            return None, "", {}, None

        sdir = supertrend_dir(df, config.TREND_SUPERTREND_PERIOD, config.TREND_SUPERTREND_MULT)
        cur, prev = int(sdir[-1]), int(sdir[-2])
        lean = 'CALL' if cur == 1 else ('PUT' if cur == -1 else None)

        # Supertrend must FLIP into a direction on the latest bar (not merely persist)
        if cur == 0 or cur == prev:
            return None, "", {}, lean

        if int(psar_dir(df)[-1]) != cur:                       # PSAR must agree
            return None, "", {}, lean

        kc = float(kaufman_chop(df, config.TREND_KAUF_N)[-1])   # kaufman-chop gate
        if not (kc <= config.TREND_KAUF_MAX):
            # A real setup formed (Supertrend flip + PSAR agree) but the chop gate blocked
            # it — return the reason (direction still None) so the bot can alert on it.
            blk = 'CALL' if cur == 1 else 'PUT'
            return None, (f"{blk} flip formed (Supertrend flip + PSAR agree) but SKIPPED: "
                          f"kauf {kc:.0f} > {config.TREND_KAUF_MAX:g} — choppy reversal, not a clean trend"), {}, lean

        direction = 'CALL' if cur == 1 else 'PUT'
        price = float(df['close'].iloc[-1])
        indicators = {
            'current_price': price, 'supertrend_dir': cur,
            'psar_dir': int(psar_dir(df)[-1]), 'kauf': round(kc, 1),
            'adx': None, 'vwap': None, 'orb_high': None, 'orb_low': None,  # audit column reuse
        }
        reason = (f"Trend {direction}: Supertrend flip {prev:+d}→{cur:+d}, PSAR agrees, "
                  f"kauf {kc:.1f} ≤ {config.TREND_KAUF_MAX:g} (price {price:.2f})")
        return direction, reason, indicators, lean
    except Exception as e:
        logger.error(f"[{symbol}] trend_entry_signal error: {e}")
        return None, "", {}, None


def trend_reversed(direction: str, df: pd.DataFrame) -> bool:
    """True if Supertrend now points AGAINST the open position — the 'trend reversed'
    exit. A CALL exits when Supertrend turns −1; a PUT exits when it turns +1."""
    try:
        need = max(config.TREND_MIN_BARS, config.TREND_SUPERTREND_PERIOD + 2)
        if df.empty or len(df) < need:
            return False
        cur = int(supertrend_dir(df, config.TREND_SUPERTREND_PERIOD,
                                 config.TREND_SUPERTREND_MULT)[-1])
        return cur == -1 if direction == 'CALL' else cur == 1
    except Exception:
        return False


def entry_realized_vol(df: pd.DataFrame) -> float:
    """Annualized realized vol from the day's closes UP TO NOW (no lookahead) — the
    entry-time volatility gate for single-leg trend (skip quiet days: a naked long on a
    dead-quiet tape just bleeds theta). Trading-minute clock, matches the backtest."""
    c = df['close'].values
    if len(c) < 3:
        return 0.0
    lr = np.diff(np.log(c))
    return float(np.nanstd(lr) * math.sqrt(252 * 390))


# ── GEX strategy entry/exit (STRATEGY='gex') ─────────────────────────────────
# Pure combiners: take the GEX levels (Gflip, OI walls) that src/gex.py computed from
# the live chain, plus 1-min bars, and apply the three-condition entry. No I/O here.

def opening_range_levels(df: pd.DataFrame, now: datetime.datetime, minutes: int):
    """High/low of the 9:30 → 9:30+minutes ET opening range (wall-clock anchored, so a
    mid-session restart still gets the right range). None if no bars in that window."""
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end_t = open_t + datetime.timedelta(minutes=minutes)
    bars = df[(df.index >= open_t) & (df.index < end_t)]
    if bars.empty:
        return None
    return float(bars['high'].max()), float(bars['low'].min())


def _accelerating(closes: pd.Series, n: int, up: bool) -> bool:
    """True if the last n bar-to-bar closes all move one way — momentum, the computable
    reading of 'delta expansion over n consecutive minutes' (delta rises as spot rises)."""
    if len(closes) < n + 1:
        return False
    seq = closes.iloc[-(n + 1):].values
    if up:
        return all(seq[i] > seq[i - 1] for i in range(1, len(seq)))
    return all(seq[i] < seq[i - 1] for i in range(1, len(seq)))


def _wall_breakout(direction: str, df: pd.DataFrame, zones: dict, tol: float):
    """True if price was recently AT a concentration wall and has now cleared it in the
    trade direction (escaping the chop zone) — the 'OR breaking out of a concentration
    zone' alternative to the negative-gamma regime gate."""
    closes = df['close']
    cur = float(closes.iloc[-1])
    look = closes.iloc[-6:-1]
    if look.empty:
        return False, None
    walls = zones.get('call_walls', []) if direction == 'CALL' else zones.get('put_walls', [])
    for w, _oi in walls:
        if direction == 'CALL' and cur > w and float(look.min()) <= w + tol:
            return True, w
        if direction == 'PUT' and cur < w and float(look.max()) >= w - tol:
            return True, w
    return False, None


def gex_entry_signal(symbol: str, df: pd.DataFrame, now: datetime.datetime,
                     gflip, zones: dict, prev_high=None, prev_low=None
                     ) -> Tuple[Optional[str], str, dict, Optional[str]]:
    """GEX entry — all three must hold at once:
      1. REGIME: spot < Gflip (negative gamma → dealers amplify) OR breaking out of a wall.
      2. BREAKOUT: 1-min close beyond the 15-min opening-range level (max/min with the
         previous session H/L when provided — must clear the more significant level).
      3. MOMENTUM: last GEX_MOMENTUM_BARS closes accelerate in the trade direction.
    Gflip/zones come from src/gex.py. Returns (direction, reason, indicators, lean).
    """
    try:
        need = max(config.GEX_OR_MINUTES + config.GEX_MOMENTUM_BARS + 2, 18)
        if df.empty or len(df) < need:
            return None, "", {}, None
        orange = opening_range_levels(df, now, config.GEX_OR_MINUTES)
        if orange is None:
            return None, "", {}, None
        or_high, or_low = orange
        closes = df['close']
        spot = float(closes.iloc[-1])
        tol = spot * config.GEX_WALL_TOL_PCT
        neg_gamma = gflip is not None and spot < gflip

        gfs = f"{gflip:.0f}" if gflip else "n/a"
        blocked = None
        for direction in ('CALL', 'PUT'):
            if direction == 'CALL':
                level = max(x for x in (or_high, prev_high) if x is not None)
                broke, up = spot > level, True
            else:
                level = min(x for x in (or_low, prev_low) if x is not None)
                broke, up = spot < level, False
            if not broke:
                continue
            # A structural breakout formed — record WHY it's blocked (regime / momentum) so
            # the bot can alert on it, for process transparency.
            wall_ok, wall_lvl = _wall_breakout(direction, df, zones, tol)
            if not (neg_gamma or wall_ok):                       # condition 1
                blocked = (f"{direction}: broke the {config.GEX_OR_MINUTES}-min OR "
                           f"({'>' if up else '<'} {level:.0f}) but SKIPPED — positive-gamma "
                           f"(spot {spot:.0f} vs Gflip {gfs}): dealers dampen, breakouts fade")
                continue
            if not _accelerating(closes, config.GEX_MOMENTUM_BARS, up):   # condition 3
                blocked = (f"{direction}: OR breakout in negative-gamma but SKIPPED — "
                           f"no momentum ({config.GEX_MOMENTUM_BARS} accelerating bars required)")
                continue
            regime = 'neg-γ (spot<Gflip)' if neg_gamma else f'wall-breakout @{wall_lvl:.0f}'
            reason = (f"GEX {direction}: {regime} | close {spot:.2f} "
                      f"{'>' if up else '<'} OR-level {level:.2f} | "
                      f"{config.GEX_MOMENTUM_BARS}-bar accel {'↑' if up else '↓'}")
            ind = {'current_price': spot, 'orb_high': or_high, 'orb_low': or_low,
                   'gflip': gflip, 'regime': 'negative' if neg_gamma else 'wall-breakout',
                   'adx': None, 'vwap': None}
            return direction, reason, ind, direction
        return None, blocked or "", {}, None
    except Exception as e:
        logger.error(f"[{symbol}] gex_entry_signal error: {e}")
        return None, "", {}, None

