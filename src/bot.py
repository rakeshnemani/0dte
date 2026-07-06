import time
from typing import Dict, Optional, Tuple
from ib_insync import IB, Stock, Option, Index, Contract, ComboLeg, LimitOrder, util
import logging
import pandas as pd
import ta
import datetime
import pytz
import os
import csv
import subprocess
import sys
import requests

import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class TradingBot:
    def __init__(self):
        self.ib = IB()
        self._connect()

        # State management — one active trade per symbol
        self.active_trades: Dict[str, dict] = {}
        self.daily_trade_count: int = 0
        self.last_trade_date: datetime.date = None
        # Cooldown: maps (symbol, direction) → datetime when the cooldown expires.
        # Replaces the old all-day block so the same signal can re-trigger after
        # SIGNAL_COOLDOWN_MINUTES (default 30) minutes, enabling continuation trades.
        self.signal_cooldowns: Dict[tuple, datetime.datetime] = {}
        self.consecutive_losses: int = 0
        self.circuit_breaker_tripped: bool = False
        # Day-level P&L tracking — populated as trades close, used for the
        # post-close day summary. Reset each new trading day.
        self.closed_trades_today: list = []
        self.daily_summary_sent: bool = False
        # Breadth data cache — TICK and VOLD are market-wide, shared across all
        # symbols. Cache for 60 seconds to avoid 3 redundant IBKR requests per loop.
        self._breadth_cache_time: Optional[datetime.datetime] = None
        self._tick_df_cache: pd.DataFrame = pd.DataFrame()
        self._vold_df_cache: pd.DataFrame = pd.DataFrame()

        # A restart wipes active_trades — adopt any open option positions the
        # account still holds so they are managed instead of orphaned (this bit
        # us on 2026-06-29 and 2026-06-30; see docs/RETROSPECTIVE.md).
        self.adopt_orphan_positions()

    def _connect(self):
        logger.info(f"Connecting to IBKR at {config.IBKR_HOST}:{config.IBKR_PORT} (clientId={config.IBKR_CLIENT_ID})")
        self.ib.connect(config.IBKR_HOST, config.IBKR_PORT, clientId=config.IBKR_CLIENT_ID)
        # Use delayed data (type 4) so paper accounts work without live subscriptions
        self.ib.reqMarketDataType(4)
        # Silence ib_insync's internal wrapper logger — it prints every IBKR error code
        # (including expected ones like 162 "no data yet") as ERROR. We handle real errors
        # ourselves via errorEvent below.
        import logging as _logging
        _logging.getLogger('ib_insync.wrapper').setLevel(_logging.CRITICAL)
        self.ib.errorEvent += self._on_ibkr_error
        # Subscribe to account positions so ib.positions() stays live — used to
        # reconcile tracked trades against the real account (detect manual closes).
        try:
            self.ib.reqPositions()
        except Exception as e:
            logger.warning(f"Could not subscribe to positions: {e}")
        logger.info("Connected to IBKR")

    # Error codes that are routine/informational and should not be logged as errors
    _IBKR_INFO_CODES = {
        162,   # HMDS no data yet — expected at/before market open
        200,   # No security definition — expected when option contract doesn't exist yet
        354,   # Requested market data is not subscribed — expected on delayed feed
        10091, # Part of market data requires additional subscription — delayed data notice
        10167, # Displaying delayed market data — expected on paper account
        10349, # Order TIF set to DAY based on preset — informational
        2104,  # Market data farm connection OK
        2106,  # HMDS data farm connection OK
        2107,  # HMDS data farm connection inactive
        2108,  # Market data farm connection inactive
        2158,  # Sec-def data farm connection OK
        2119,  # Market data farm is connecting
    }

    def _on_ibkr_error(self, reqId: int, errorCode: int, errorString: str, contract):
        """Route IBKR error events: suppress expected codes, log real problems."""
        if errorCode in self._IBKR_INFO_CODES:
            logger.debug(f"IBKR [{errorCode}] reqId={reqId}: {errorString}")
        else:
            logger.warning(f"IBKR error [{errorCode}] reqId={reqId}: {errorString}")

    def _ensure_connected(self):
        if not self.ib.isConnected():
            logger.warning("IBKR connection lost. Reconnecting...")
            try:
                self._connect()
            except Exception as e:
                logger.error(f"Reconnect failed: {e}")

    # ── Contract helpers ─────────────────────────────────────────────────────

    def _underlying_contract(self, symbol: str):
        """Return the correct IBKR contract type for an underlying symbol."""
        if symbol == 'SPX':
            return Index('SPX', 'CBOE', 'USD')
        return Stock(symbol, 'SMART', 'USD')

    def _option_symbol(self, symbol: str) -> str:
        """Map 0DTE underlying symbol to the correct IBKR option root (SPX → SPXW)."""
        return 'SPXW' if symbol == 'SPX' else symbol

    def _get_option_contract(self, symbol: str, direction: str, strike: float) -> Option:
        today_str = datetime.datetime.now(pytz.timezone('America/New_York')).strftime('%Y%m%d')
        right = 'C' if direction == 'CALL' else 'P'
        contract = Option(self._option_symbol(symbol), today_str, strike, right, 'SMART')
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError(f"Cannot qualify option: {self._option_symbol(symbol)} {today_str} {right} {strike}")
        return qualified[0]

    # ── Market data helpers ──────────────────────────────────────────────────

    def _ticker_mid(self, ticker) -> float:
        """Return mid-price from a ticker, falling back through bid → last → close."""
        def valid(v):
            return v is not None and v == v and v > 0  # truthy and not NaN
        bid = float(ticker.bid) if valid(ticker.bid) else 0.0
        ask = float(ticker.ask) if valid(ticker.ask) else 0.0
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        for fallback in (ticker.last, ticker.close):
            v = float(fallback) if valid(fallback) else 0.0
            if v > 0:
                return v
        return 0.0

    def _request_snapshot(self, contract) -> object:
        """Subscribe to market data, wait for snapshot, cancel subscription."""
        ticker = self.ib.reqMktData(contract, '', snapshot=False, regulatorySnapshot=False)
        self.ib.sleep(2)
        self.ib.cancelMktData(contract)
        return ticker

    # ── Notifications ────────────────────────────────────────────────────────

    def send_discord_alert(self, title: str, description: str, color: int):
        if not config.DISCORD_WEBHOOK_URL:
            return
        payload = {"embeds": [{"title": title, "description": description, "color": color}]}
        try:
            response = requests.post(config.DISCORD_WEBHOOK_URL, json=payload, timeout=5)
            if response.status_code not in (200, 204):
                logger.error(f"Discord alert failed: HTTP {response.status_code}")
        except Exception as e:
            logger.error(f"Discord alert failed: {e}")

    # ── Audit log ────────────────────────────────────────────────────────────

    def log_audit(self, action: str, symbol: str, direction: str, price: float, reason: str,
                  adx: Optional[float] = None, vwap: Optional[float] = None,
                  orb_high: Optional[float] = None, orb_low: Optional[float] = None,
                  underlying_price: Optional[float] = None, profit_pct: Optional[float] = None,
                  dollar_pnl: Optional[float] = None, breadth: Optional[str] = None,
                  adx_slope: Optional[float] = None, peak_pct: Optional[float] = None):
        file_exists = os.path.isfile("audit.csv")
        try:
            with open("audit.csv", mode='a', newline='') as file:
                writer = csv.writer(file)
                if not file_exists:
                    writer.writerow([
                        "Timestamp", "Action", "Symbol", "Direction", "Price", "Underlying_Price",
                        "ADX", "VWAP", "ORB_High", "ORB_Low", "Breadth", "Reason",
                        "Profit_Pct", "Dollar_PnL", "ADX_Slope", "Peak_Pct"
                    ])
                writer.writerow([
                    # Log in ET — the timezone the strategy runs in
                    datetime.datetime.now(pytz.timezone('America/New_York')).strftime("%Y-%m-%d %H:%M:%S"),
                    action, symbol, direction, price,
                    f"{underlying_price:.2f}" if underlying_price else "",
                    f"{adx:.2f}" if adx else "",
                    f"{vwap:.2f}" if vwap else "",
                    f"{orb_high:.2f}" if orb_high else "",
                    f"{orb_low:.2f}" if orb_low else "",
                    breadth or "",
                    reason,
                    f"{profit_pct*100:.2f}%" if profit_pct is not None else "",
                    f"{dollar_pnl:.2f}" if dollar_pnl is not None else "",
                    f"{adx_slope:+.2f}" if adx_slope is not None else "",
                    f"{peak_pct*100:.2f}%" if peak_pct is not None else ""
                ])
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")

    # ── Daily reset ──────────────────────────────────────────────────────────

    def check_and_reset_daily_trade_count(self) -> None:
        today = datetime.datetime.now(pytz.timezone('America/New_York')).date()
        if self.last_trade_date != today:
            self.daily_trade_count = 0
            self.last_trade_date = today
            self.signal_cooldowns.clear()
            self.consecutive_losses = 0
            self.circuit_breaker_tripped = False
            self.closed_trades_today = []
            self.daily_summary_sent = False
            logger.info(f"Daily trade count reset for {today}")

    # ── Market hours ─────────────────────────────────────────────────────────

    def is_market_open(self) -> bool:
        now = datetime.datetime.now(pytz.timezone('America/New_York'))
        if now.weekday() >= 5:
            return False
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= now <= market_close

    def _is_entry_window(self) -> bool:
        now = datetime.datetime.now(pytz.timezone('America/New_York'))
        return now.hour < 15

    def _is_eod_flatten_time(self) -> bool:
        """True once we've reached the end-of-day flatten time (ET) on a trading day."""
        now = datetime.datetime.now(pytz.timezone('America/New_York'))
        flatten_at = now.replace(hour=config.EOD_FLATTEN_HOUR,
                                 minute=config.EOD_FLATTEN_MINUTE,
                                 second=0, microsecond=0)
        return now >= flatten_at

    def _seconds_until_market_open(self) -> int:
        """Return seconds until the next 9:30 AM EST weekday open."""
        now = datetime.datetime.now(pytz.timezone('America/New_York'))
        next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now >= next_open:
            next_open += datetime.timedelta(days=1)
        while next_open.weekday() >= 5:   # skip Saturday (5) and Sunday (6)
            next_open += datetime.timedelta(days=1)
        return max(int((next_open - now).total_seconds()), 60)

    # ── Broker API calls ─────────────────────────────────────────────────────

    def get_current_price(self, symbol: str) -> float:
        """Fetch latest price for an underlying symbol from IBKR."""
        try:
            contract = self._underlying_contract(symbol)
            self.ib.qualifyContracts(contract)
            ticker = self._request_snapshot(contract)
            price = self._ticker_mid(ticker)
            return price
        except Exception as e:
            logger.error(f"Error fetching price for {symbol}: {e}")
            return 0.0

    def fetch_intraday_data(self, symbol: str) -> pd.DataFrame:
        """Fetch 1-minute bars for the current trading day from IBKR."""
        try:
            contract = self._underlying_contract(symbol)
            self.ib.qualifyContracts(contract)

            # SPX is a cash index — use MIDPOINT since it has no "trade" volume
            what_to_show = 'MIDPOINT' if symbol == 'SPX' else 'TRADES'
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime='',
                durationStr='1 D',
                barSizeSetting='1 min',
                whatToShow=what_to_show,
                useRTH=True,
                formatDate=1,
                timeout=30,
            )
            if not bars:
                return pd.DataFrame()

            df = util.df(bars)
            df['date'] = pd.to_datetime(df['date'])
            if df['date'].dt.tz is None:
                df['date'] = df['date'].dt.tz_localize('America/New_York')
            else:
                df['date'] = df['date'].dt.tz_convert('America/New_York')
            df = df.set_index('date')

            # Keep only today's RTH bars (IBKR durationStr='1 D' can include
            # yesterday's bars; delayed data also backfills with NaN rows)
            now = datetime.datetime.now(pytz.timezone('America/New_York'))
            today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            df = df[df.index >= today_open]

            # Drop rows where key price fields are NaN (delayed feed backfill artefacts)
            df = df.dropna(subset=['open', 'high', 'low', 'close'])

            # SPX MIDPOINT bars have no volume; set dummy volume=1 so VWAP math works
            if 'volume' not in df.columns or (df['volume'] == 0).all():
                df['volume'] = 1

            # Stale-feed detector: on 2026-07-01 an entry was evaluated on
            # indicator values identical to ~2h-old data. Flag it if it recurs.
            if not df.empty and self.is_market_open():
                bar_age = (now - df.index[-1]).total_seconds()
                if bar_age > 600:
                    logger.warning(
                        f"[{symbol}] Intraday bars may be STALE: last bar is "
                        f"{bar_age/60:.0f} min old. Indicators may be unreliable."
                    )

            return df[['open', 'high', 'low', 'close', 'volume']]
        except Exception as e:
            logger.error(f"Failed to fetch intraday data for {symbol}: {e}")
            return pd.DataFrame()

    def fetch_breadth_data(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Fetch $TICK and $VOLD 1-min bars from IBKR, cached for 60 seconds.

        These are NYSE market-breadth indices, shared across all symbols — no need
        to re-fetch for each symbol inside the same loop iteration.

        Returns (tick_df, vold_df). On any error returns two empty DataFrames so
        the caller can fail-open and not block the trade.
        """
        now = datetime.datetime.now(pytz.timezone('America/New_York'))
        if (self._breadth_cache_time is not None and
                (now - self._breadth_cache_time).total_seconds() < 60):
            return self._tick_df_cache, self._vold_df_cache

        try:
            today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)

            def _fetch_index(contract):
                bars = self.ib.reqHistoricalData(
                    contract, endDateTime='', durationStr='1 D',
                    barSizeSetting='1 min', whatToShow='TRADES',
                    useRTH=True, formatDate=1, timeout=30)
                if not bars:
                    return pd.DataFrame()
                df = util.df(bars)
                df['date'] = pd.to_datetime(df['date'])
                if df['date'].dt.tz is None:
                    df['date'] = df['date'].dt.tz_localize('America/New_York')
                else:
                    df['date'] = df['date'].dt.tz_convert('America/New_York')
                df = df.set_index('date')
                return df[df.index >= today_open].dropna(subset=['close'])

            tick_df = _fetch_index(Index('TICK', 'NYSE', 'USD'))
            vold_df = _fetch_index(Index('VOLD', 'NYSE', 'USD'))
            self._breadth_cache_time = now
            self._tick_df_cache = tick_df
            self._vold_df_cache = vold_df
            return tick_df, vold_df
        except Exception as e:
            logger.warning(f"Failed to fetch breadth data: {e}")
            return pd.DataFrame(), pd.DataFrame()

    def _breadth_confirms(self, direction: str, tick_df: pd.DataFrame, vold_df: pd.DataFrame) -> Tuple[bool, str]:
        """
        Check whether NYSE breadth (TICK + VOLD) confirms the trade direction.

        Returns (confirmed, reason_string). Fails open: insufficient data → True.

        CALL: VOLD slope > 0 (up-volume rising) AND TICK higher lows ≥ 60% of
              recent bars AND avg TICK > -200 (not dominated by downticks)
        PUT:  VOLD slope < 0 (up-volume falling) AND TICK NOT making higher lows
              AND avg TICK < 200 (not dominated by upticks)
        """
        N = 10  # look back 10 bars (~10 minutes)
        if tick_df.empty or vold_df.empty or len(tick_df) < N or len(vold_df) < N:
            return True, "breadth data insufficient — filter bypassed"

        tick_close = tick_df['close'].tail(N)
        tick_lows  = tick_df['low'].tail(N) if 'low' in tick_df.columns else tick_close
        vold_close = vold_df['close'].tail(N)

        avg_tick   = float(tick_close.mean())
        vold_slope = float(vold_close.iloc[-1] - vold_close.iloc[0])

        lows = tick_lows.values
        higher_lows = sum(1 for i in range(1, len(lows)) if lows[i] >= lows[i - 1])
        tick_hl = higher_lows >= (len(lows) - 1) * 0.6   # ≥60% of consecutive pairs

        if direction == 'CALL':
            confirmed = (vold_slope > 0) and tick_hl and (avg_tick > -200)
        else:
            confirmed = (vold_slope < 0) and (not tick_hl) and (avg_tick < 200)

        reason = (
            f"VOLD slope {vold_slope:+.0f} | TICK avg {avg_tick:+.0f} | "
            f"Higher lows {'✓' if tick_hl else '✗'} ({higher_lows}/{len(lows)-1})"
        )
        return confirmed, reason

    def _get_option_mid(self, contract: Option) -> float:
        """Fetch the mid-price (bid/ask midpoint) for a single option leg."""
        try:
            ticker = self._request_snapshot(contract)
            return self._ticker_mid(ticker)
        except Exception as e:
            logger.error(f"Error fetching option mid price: {e}")
            return 0.0

    def get_spread_value(self, symbol: str, direction: str, long_strike: float, short_strike: float) -> float:
        """Fetch the current market value of the debit spread from IBKR."""
        try:
            long_c = self._get_option_contract(symbol, direction, long_strike)
            short_c = self._get_option_contract(symbol, direction, short_strike)
            long_mid = self._get_option_mid(long_c)
            short_mid = self._get_option_mid(short_c)
            if long_mid <= 0 or short_mid <= 0:
                logger.warning(f"Could not price {symbol} {direction} {long_strike}/{short_strike}: long_mid={long_mid} short_mid={short_mid}")
                return 0.0
            return max(long_mid - short_mid, 0.01)
        except Exception as e:
            logger.error(f"Error fetching spread value for {symbol}: {e}")
            return 0.0

    # ── Strategy ─────────────────────────────────────────────────────────────

    def evaluate_entry_strategy(self, symbol: str) -> Tuple[Optional[str], str, dict]:
        """
        Calculate VWAP, 30-min ORB, and ADX to determine trade direction.
        Returns (direction, reason, indicators) where direction is 'CALL'/'PUT'/None.
        """
        now = datetime.datetime.now(pytz.timezone('America/New_York'))
        df = self.fetch_intraday_data(symbol)
        # ADX(14) in the ta library needs ~2×window+1 bars — with fewer it raises
        # "index 14 is out of bounds" (seen every morning ~09:50–09:58 ET at 20–28 bars).
        if df.empty or len(df) < 30:
            return None, "", {}

        try:
            vwap_indicator = ta.volume.VolumeWeightedAveragePrice(
                high=df['high'], low=df['low'], close=df['close'], volume=df['volume']
            )
            df.loc[:, 'VWAP'] = vwap_indicator.volume_weighted_average_price()

            adx_indicator = ta.trend.ADXIndicator(
                high=df['high'], low=df['low'], close=df['close'], window=14
            )
            df.loc[:, 'ADX'] = adx_indicator.adx()

            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            orb_end_time = market_open + datetime.timedelta(minutes=30)
            orb_bars = df[(df.index >= market_open) & (df.index < orb_end_time)]
            if orb_bars.empty:
                return None, "", {}

            orb_high = orb_bars['high'].max()
            orb_low = orb_bars['low'].min()

            current_close = df['close'].iloc[-1]
            current_vwap = df['VWAP'].iloc[-1]
            current_adx = df['ADX'].iloc[-1]

            if pd.isna(current_adx) or current_adx < 25:
                return None, "", {}

            # ── Chop guard 1: ADX must be RISING, not just above 25 ───────────
            # 2026-07-01: ADX direction predicted all 5 outcomes — every hard-stop
            # loser entered on flat/fading ADX that then collapsed. A level check
            # passes on residual momentum; the slope says the trend is still alive.
            # Fails open early in the session when the lookback bar is still NaN.
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
                        return None, "", {}

            indicators = {
                'adx': current_adx, 'vwap': current_vwap,
                'orb_high': orb_high, 'orb_low': orb_low, 'current_price': current_close,
                'adx_slope': adx_slope,
            }

            # ── Chop guard 2: breakout must CLEAR the ORB level, not poke it ──
            # 2026-07-01's QQQ loser entered 0.15% above ORB high — noise, not a
            # breakout. Require the close to exceed the level by a small buffer.
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
                return None, "", {}

            # ── Breadth annotation (logged, not a gate) ───────────────────────
            # Fetch $TICK and $VOLD — cached 60s so all 3 symbols share one pull.
            # The reading is logged and written to audit.csv for post-trade analysis.
            # It does NOT block the trade — collect data first, add a hard filter
            # only once paper-trade results show a real correlation with losses.
            tick_df, vold_df = self.fetch_breadth_data()
            confirmed, breadth_reason = self._breadth_confirms(direction, tick_df, vold_df)
            breadth_label = "✓ confirmed" if confirmed else "✗ diverging"
            indicators['breadth'] = f"{breadth_label} | {breadth_reason}"
            logger.info(f"[{symbol}] Breadth ({direction}): {breadth_label} — {breadth_reason}")
            return direction, reason, indicators

        except Exception as e:
            logger.error(f"Error calculating indicators for {symbol}: {e}")

        return None, "", {}

    # ── Trade execution ──────────────────────────────────────────────────────

    def execute_trade(self, symbol: str, direction: str, reason: str, indicators: dict):
        """Execute an ATM debit spread via IBKR BAG combo order."""
        if symbol in self.active_trades:
            logger.warning(f"Already in active trade for {symbol}. Skipping.")
            return

        now_est = datetime.datetime.now(pytz.timezone('America/New_York'))
        cooldown_expires = self.signal_cooldowns.get((symbol, direction))
        if cooldown_expires and now_est < cooldown_expires:
            remaining = int((cooldown_expires - now_est).total_seconds() // 60)
            logger.info(f"Signal ({symbol}, {direction}) in cooldown for {remaining}m more. Skipping.")
            return

        if self.circuit_breaker_tripped:
            logger.warning(f"Circuit breaker active — {self.consecutive_losses} consecutive losses. No new entries today.")
            return

        if self.daily_trade_count >= config.MAX_TRADES_PER_DAY:
            logger.warning(f"Daily trade limit of {config.MAX_TRADES_PER_DAY} reached.")
            return

        underlying_price = self.get_current_price(symbol)
        if underlying_price <= 0:
            return

        step = config.STRIKE_STEP.get(symbol, 1)
        atm_strike = round(underlying_price / step) * step
        strike_width = config.SPREAD_WIDTH.get(symbol, 1)

        if direction == "CALL":
            long_strike, short_strike = atm_strike, atm_strike + strike_width
        else:
            long_strike, short_strike = atm_strike, atm_strike - strike_width

        spread_cost = self.get_spread_value(symbol, direction, long_strike, short_strike)
        if spread_cost <= 0:
            logger.warning(f"Could not fetch valid spread cost for {symbol}. Aborting.")
            return

        if spread_cost < config.MIN_SPREAD_COST:
            logger.warning(f"Spread cost ${spread_cost:.2f} below minimum ${config.MIN_SPREAD_COST:.2f}. Skipping.")
            return

        qty_to_buy = int(config.MAX_POSITION_SIZE // (spread_cost * 100))
        if qty_to_buy < 1:
            logger.warning(f"Spread cost ${spread_cost:.2f}/share exceeds max position size ${config.MAX_POSITION_SIZE}.")
            return

        try:
            long_c = self._get_option_contract(symbol, direction, long_strike)
            short_c = self._get_option_contract(symbol, direction, short_strike)

            bag = Contract()
            bag.symbol = self._option_symbol(symbol)
            bag.secType = 'BAG'
            bag.currency = 'USD'
            bag.exchange = 'SMART'
            bag.comboLegs = [
                ComboLeg(conId=long_c.conId, ratio=1, action='BUY', exchange='SMART'),
                ComboLeg(conId=short_c.conId, ratio=1, action='SELL', exchange='SMART'),
            ]

            order = LimitOrder('BUY', qty_to_buy, round(spread_cost, 2))
            ibkr_trade = self.ib.placeOrder(bag, order)

            self.active_trades[symbol] = {
                'direction': direction,
                'target_entry_price': spread_cost,
                'status': 'PENDING_ENTRY',
                'ibkr_trade': ibkr_trade,
                'bag_contract': bag,
                'qty': qty_to_buy,
                'max_profit_pct': 0.0,
                'long_strike': long_strike,
                'short_strike': short_strike,
                'long_conid': long_c.conId,    # for position reconciliation
                'short_conid': short_c.conId,
                'entry_indicators': indicators,
                'reason': reason,
            }
            self.daily_trade_count += 1
            cooldown_until = now_est + datetime.timedelta(minutes=config.SIGNAL_COOLDOWN_MINUTES)
            self.signal_cooldowns[(symbol, direction)] = cooldown_until
            logger.info(f"Signal ({symbol}, {direction}) cooling down until {cooldown_until.strftime('%H:%M')} EST.")
            logger.info(
                f"SUBMITTED {direction} SPREAD ORDER to IBKR: {qty_to_buy} contracts {symbol} "
                f"at LIMIT ${spread_cost:.2f} (OrderId: {ibkr_trade.order.orderId})"
            )

            # Fire Discord alert immediately on submission so you can monitor
            # even if the order gets rejected before fill confirmation.
            adx_v = indicators.get('adx', 0)
            vwap_v = indicators.get('vwap', 0)
            price_v = indicators.get('current_price', 0)
            orb_h = indicators.get('orb_high', 0)
            orb_l = indicators.get('orb_low', 0)
            breadth_line = indicators.get('breadth', '')
            submit_desc = (
                f"**📊 Ticker:** {symbol}\n"
                f"**🎯 Direction:** {direction} Spread\n"
                f"**⚙️ Strikes:** Long ${long_strike:.0f} / Short ${short_strike:.0f}\n"
                f"**💰 Limit Price:** ${spread_cost:.2f} per contract\n"
                f"**📈 Quantity:** {qty_to_buy} contracts\n"
                f"**💸 Max Investment:** ${spread_cost * qty_to_buy * 100:.2f}\n\n"
                f"**📉 Indicators at Signal:**\n"
                f"• ADX: {adx_v:.2f}\n"
                f"• Price vs VWAP: ${price_v:.2f} / ${vwap_v:.2f}\n"
                f"• ORB: High ${orb_h:.2f} / Low ${orb_l:.2f}\n"
                + (f"• Breadth: {breadth_line}\n" if breadth_line else "") +
                f"\n**📝 Signal:** {reason}\n"
                f"**⏳ Status:** Pending fill (Order #{ibkr_trade.order.orderId})"
            )
            self.send_discord_alert("⏳ ORDER SUBMITTED — Awaiting Fill", submit_desc, 0xF39C12)

            # Send the consolidated "today" snapshot after every new trade
            self.send_today_summary()

        except Exception as e:
            logger.error(f"Failed to place IBKR spread order for {symbol}: {e}")

    # ── Exit management ──────────────────────────────────────────────────────

    def evaluate_exit_conditions(self):
        for symbol in list(self.active_trades.keys()):
            self.evaluate_exit_conditions_for_symbol(symbol)

    def _position_still_open(self, trade) -> bool:
        """Return True if the spread's long leg is still held in the IBKR account.

        Fails open (returns True) whenever the answer can't be determined, so a
        data hiccup never causes the bot to abandon a real open position.
        """
        long_conid = trade.get('long_conid')
        if long_conid is None:
            return True  # nothing to match against — assume open

        # Grace period: give the account feed time to reflect a just-filled entry
        # before this leg could be (wrongly) reported as missing.
        activated_at = trade.get('activated_at')
        if activated_at is not None:
            age = (datetime.datetime.now(pytz.timezone('America/New_York')) - activated_at).total_seconds()
            if age < 90:
                return True

        try:
            positions = self.ib.positions()
        except Exception as e:
            logger.warning(f"Position reconciliation fetch failed: {e}")
            return True  # fail-open
        for p in positions:
            if p.contract.conId == long_conid and p.position != 0:
                return True
        return False

    def adopt_orphan_positions(self):
        """At startup, adopt open option spreads the account holds but the bot
        isn't tracking (orphaned by a restart). Adopted trades become ACTIVE and
        are managed by the normal exit rules and EOD flatten. Positions that
        can't be paired into a spread (or aren't 0DTE) are alerted, not adopted."""
        try:
            positions = self.ib.positions()
        except Exception as e:
            logger.warning(f"Startup position scan failed: {e}")
            return

        today_str = datetime.datetime.now(pytz.timezone('America/New_York')).strftime('%Y%m%d')
        opts = [p for p in positions if p.contract.secType == 'OPT' and p.position != 0]
        if not opts:
            return

        # Group today's option legs by (underlying, right)
        groups: Dict[tuple, list] = {}
        leftovers = []
        for p in opts:
            c = p.contract
            underlying = 'SPX' if c.symbol == 'SPXW' else c.symbol
            if c.lastTradeDateOrContractMonth != today_str:
                leftovers.append(p)
                continue
            groups.setdefault((underlying, c.right), []).append(p)

        adopted = []
        for (underlying, right), legs in groups.items():
            longs = [p for p in legs if p.position > 0]
            shorts = [p for p in legs if p.position < 0]
            if len(longs) != 1 or len(shorts) != 1 or underlying in self.active_trades:
                leftovers.extend(legs)
                continue
            long_p, short_p = longs[0], shorts[0]
            qty = int(min(long_p.position, -short_p.position))
            # avgCost for options is premium per contract (price × 100)
            entry_price = max((long_p.avgCost - short_p.avgCost) / 100.0, 0.01)
            direction = 'CALL' if right == 'C' else 'PUT'

            bag = Contract()
            bag.symbol = long_p.contract.symbol
            bag.secType = 'BAG'
            bag.currency = 'USD'
            bag.exchange = 'SMART'
            bag.comboLegs = [
                ComboLeg(conId=long_p.contract.conId, ratio=1, action='BUY', exchange='SMART'),
                ComboLeg(conId=short_p.contract.conId, ratio=1, action='SELL', exchange='SMART'),
            ]

            self.active_trades[underlying] = {
                'direction': direction,
                'target_entry_price': entry_price,
                'entry_price': entry_price,
                'status': 'ACTIVE',
                'activated_at': datetime.datetime.now(pytz.timezone('America/New_York')),
                'bag_contract': bag,
                'qty': qty,
                'max_profit_pct': 0.0,
                'long_strike': float(long_p.contract.strike),
                'short_strike': float(short_p.contract.strike),
                'long_conid': long_p.contract.conId,
                'short_conid': short_p.contract.conId,
                'entry_indicators': {},
                'reason': 'Adopted at startup (position found in account, not in bot state)',
            }
            adopted.append(f"• {underlying} {direction}  {qty}x  "
                           f"${long_p.contract.strike:.0f}/${short_p.contract.strike:.0f}  "
                           f"entry ≈ ${entry_price:.2f}")
            logger.warning(f"[{underlying}] Adopted orphaned {direction} spread "
                           f"({qty}x {long_p.contract.strike}/{short_p.contract.strike}, "
                           f"entry ≈ ${entry_price:.2f}) — now managed by exit rules.")

        if adopted:
            self.send_discord_alert(
                "🔁 ADOPTED ORPHANED POSITIONS",
                "Found open spreads in the account that the bot wasn't tracking "
                "(likely a restart). Now managed by the normal exit rules:\n\n"
                + "\n".join(adopted)
                + "\n\n_Entry prices estimated from account avgCost; P&L % is relative to that._",
                0xE67E22
            )
        if leftovers:
            lines = [f"• {p.contract.localSymbol or p.contract.symbol}  pos {p.position:+.0f}" for p in leftovers]
            logger.warning(f"Unadoptable positions found at startup: {len(leftovers)}")
            self.send_discord_alert(
                "⚠️ UNTRACKED POSITIONS NEED ATTENTION",
                "These account positions could not be adopted (not a clean 0DTE spread "
                "pair). The bot will NOT manage them — review manually:\n\n" + "\n".join(lines),
                0xE74C3C
            )

    def evaluate_exit_conditions_for_symbol(self, symbol: str):
        if symbol not in self.active_trades:
            return

        trade = self.active_trades[symbol]

        # Reconcile against the real IBKR account. If an ACTIVE position is no
        # longer held (closed manually via Client Portal / mobile / TWS, assigned, etc.), stop
        # tracking it instead of trying to manage or sell a position we don't own.
        # Require two consecutive "missing" reads so a transient empty snapshot
        # can't drop a live trade.
        if trade.get('status') == 'ACTIVE':
            if self._position_still_open(trade):
                trade['reconcile_misses'] = 0
            else:
                trade['reconcile_misses'] = trade.get('reconcile_misses', 0) + 1
                if trade['reconcile_misses'] >= 2:
                    logger.warning(f"[{symbol}] Position no longer in IBKR account — closed externally. Dropping from tracking.")
                    self.send_discord_alert(
                        "⚠️ POSITION CLOSED EXTERNALLY",
                        f"**{symbol} {trade['direction']} Spread** is no longer held in your IBKR "
                        f"account — it was closed outside the bot (Client Portal, mobile app, or TWS).\n"
                        f"Removed from tracking; the bot will not manage it or record a P&L for it.",
                        0xE67E22
                    )
                    self.active_trades.pop(symbol, None)
                else:
                    logger.info(f"[{symbol}] Position not found in account (miss {trade['reconcile_misses']}/2); re-checking next loop.")
                return  # skip exit eval while the position's status is uncertain/closed

        current_spread_value = self.get_spread_value(
            symbol, trade['direction'], trade['long_strike'], trade['short_strike']
        )
        if current_spread_value <= 0:
            logger.warning(f"Could not fetch spread value for {symbol}. Skipping exit eval.")
            return

        if trade.get('status') == 'PENDING_ENTRY':
            try:
                self.ib.sleep(0)  # flush event loop so orderStatus is current
                ibkr_trade = trade['ibkr_trade']
                status = ibkr_trade.orderStatus.status

                if status == 'Filled':
                    filled_price = float(ibkr_trade.orderStatus.avgFillPrice)
                    logger.info(f"[{symbol}] IBKR ORDER FILLED at ${filled_price:.2f}")
                    trade['status'] = 'ACTIVE'
                    trade['entry_price'] = filled_price
                    # Mark fill time — reconciliation gives the account a grace
                    # period to reflect the new position before it can be flagged closed.
                    trade['activated_at'] = datetime.datetime.now(pytz.timezone('America/New_York'))
                    self.log_audit(
                        "BUY", symbol, trade['direction'], filled_price, trade.get('reason', ''),
                        adx=trade['entry_indicators'].get('adx'),
                        vwap=trade['entry_indicators'].get('vwap'),
                        orb_high=trade['entry_indicators'].get('orb_high'),
                        orb_low=trade['entry_indicators'].get('orb_low'),
                        underlying_price=trade['entry_indicators'].get('current_price'),
                        breadth=trade['entry_indicators'].get('breadth'),
                        adx_slope=trade['entry_indicators'].get('adx_slope')
                    )
                    ind = trade['entry_indicators']
                    breadth_entry = ind.get('breadth', '')
                    desc = (
                        f"**📊 Ticker:** {symbol}\n"
                        f"**🎯 Direction:** {trade['direction']} Spread\n"
                        f"**⚙️ Strikes:** Long ${trade['long_strike']:.2f} / Short ${trade['short_strike']:.2f}\n"
                        f"**💰 Entry Price:** ${filled_price:.2f} per contract\n"
                        f"**📈 Quantity:** {trade['qty']} Contracts\n"
                        f"**💸 Total Investment:** ${filled_price * trade['qty'] * 100:.2f}\n\n"
                        f"**📉 Indicators at Entry:**\n"
                        f"• ADX: {ind.get('adx', 0):.2f}\n"
                        f"• Price vs VWAP: ${ind.get('current_price', 0):.2f} / ${ind.get('vwap', 0):.2f}\n"
                        f"• ORB: High ${ind.get('orb_high', 0):.2f} / Low ${ind.get('orb_low', 0):.2f}\n"
                        + (f"• Breadth: {breadth_entry}\n" if breadth_entry else "") +
                        f"\n**📝 Reason:** {trade.get('reason', 'N/A')}"
                    )
                    self.send_discord_alert("🟢 NEW 0DTE SPREAD ENTRY", desc, 0x2ECC71)

                elif status in ('Cancelled', 'ApiCancelled', 'Inactive'):
                    logger.warning(f"[{symbol}] IBKR order {status}. Removing from tracking.")
                    self.active_trades.pop(symbol, None)
                else:
                    logger.info(f"[{symbol}] IBKR order still pending (Status: {status}).")
            except Exception as e:
                logger.error(f"[{symbol}] Error checking IBKR order status: {e}")
            return

        profit_pct = (current_spread_value - trade['entry_price']) / trade['entry_price']

        # Cache live P&L so the "today" summary can read it without extra IBKR calls
        trade['current_value'] = current_spread_value
        trade['current_profit_pct'] = profit_pct

        if profit_pct > trade['max_profit_pct']:
            trade['max_profit_pct'] = profit_pct
            if profit_pct > 0:
                logger.info(f"[{symbol}] New Max Profit: {profit_pct*100:.2f}%")

        exit_triggered = False
        exit_reason = ""

        # Three-rule exit model:
        #   1. Hard stop — cap the downside at HARD_STOP_LOSS_PCT (70%).
        #   2. Thesis invalidation — the entry reason is "price beyond VWAP and
        #      the ORB level". If price closes back on the WRONG side of VWAP for
        #      VWAP_INVALIDATION_BARS consecutive 1-min bars, the reason for being
        #      in the trade is gone — exit instead of riding to the hard stop.
        #      (2026-07-01: all three −70% losers were below VWAP long before the
        #      stop hit; this rule would have cut them near −20/−30%.)
        #   3. Trailing stop — does NOTHING until the trade has peaked at
        #      TAKE_PROFIT_TRAIL_TRIGGER (50%). Once armed, exit if profit falls
        #      to (1 - TRAILING_STOP_LOSS_PCT) of the peak (peak +50% -> exit +45%).

        # Rule 2 precompute: check for a sustained VWAP recross
        thesis_invalidated = False
        if config.VWAP_INVALIDATION_BARS > 0:
            try:
                df = self.fetch_intraday_data(symbol)
                n = config.VWAP_INVALIDATION_BARS
                if not df.empty and len(df) >= max(20, n):
                    vwap_series = ta.volume.VolumeWeightedAveragePrice(
                        high=df['high'], low=df['low'], close=df['close'], volume=df['volume']
                    ).volume_weighted_average_price()
                    closes = df['close'].tail(n)
                    vwaps = vwap_series.tail(n)
                    if trade['direction'] == 'CALL':
                        thesis_invalidated = bool((closes < vwaps).all())
                    else:
                        thesis_invalidated = bool((closes > vwaps).all())
            except Exception as e:
                logger.warning(f"[{symbol}] VWAP invalidation check failed: {e}")

        # Rule 1: Hard stop loss
        if profit_pct <= -config.HARD_STOP_LOSS_PCT:
            exit_triggered = True
            exit_reason = f"Hard stop loss: spread lost {abs(profit_pct)*100:.1f}% of entry value"

        # Rule 2: Thesis invalidation — sustained VWAP recross against the trade
        elif thesis_invalidated:
            side = "below" if trade['direction'] == 'CALL' else "above"
            exit_triggered = True
            exit_reason = (
                f"Thesis invalidated: price closed {side} VWAP for "
                f"{config.VWAP_INVALIDATION_BARS} consecutive bars (Current P&L: {profit_pct*100:+.2f}%)"
            )

        # Rule 3: Trailing stop, armed only after reaching +50% peak
        elif trade['max_profit_pct'] >= config.TAKE_PROFIT_TRAIL_TRIGGER:
            trailing_threshold = trade['max_profit_pct'] * (1 - config.TRAILING_STOP_LOSS_PCT)
            if profit_pct <= trailing_threshold:
                exit_triggered = True
                exit_reason = (
                    f"Trailing stop after +{config.TAKE_PROFIT_TRAIL_TRIGGER*100:.0f}% peak: "
                    f"gave back to {profit_pct*100:.2f}% "
                    f"(Peak: {trade['max_profit_pct']*100:.2f}%, Threshold: {trailing_threshold*100:.2f}%)"
                )

        if exit_triggered:
            logger.info(f"[{symbol}] EXIT TRIGGERED: {exit_reason}")
            self.close_position(symbol, current_spread_value, exit_reason)

    def close_position(self, symbol: str, current_spread_value: float, reason: str):
        """Submit a closing BAG order for the active spread position."""
        if symbol not in self.active_trades:
            return

        trade = self.active_trades[symbol]
        try:
            # Use the same BAG contract structure — a SELL order closes the position
            closing_order = LimitOrder('SELL', trade['qty'], round(current_spread_value, 2))
            exit_ibkr_trade = self.ib.placeOrder(trade['bag_contract'], closing_order)
            logger.info(
                f"[{symbol}] SUBMITTED CLOSING SPREAD ORDER to IBKR at LIMIT "
                f"${current_spread_value:.2f} (OrderId: {exit_ibkr_trade.order.orderId})"
            )

            profit_pct = (current_spread_value - trade['entry_price']) / trade['entry_price']
            dollar_pnl = (current_spread_value - trade['entry_price']) * trade['qty'] * 100

            # Record for the post-close day summary
            self.closed_trades_today.append({
                'symbol': symbol, 'direction': trade['direction'],
                'entry_price': trade['entry_price'], 'exit_price': current_spread_value,
                'profit_pct': profit_pct, 'dollar_pnl': dollar_pnl, 'reason': reason,
            })

            # Update circuit breaker counter
            if profit_pct < 0:
                self.consecutive_losses += 1
                if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES and not self.circuit_breaker_tripped:
                    self.circuit_breaker_tripped = True
                    logger.warning(
                        f"CIRCUIT BREAKER TRIPPED: {self.consecutive_losses} consecutive losses. "
                        f"No new entries for the rest of the day."
                    )
                    self.send_discord_alert(
                        "🚨 CIRCUIT BREAKER TRIPPED",
                        f"**{self.consecutive_losses} consecutive losing trades.**\nNo new entries will be placed for the rest of today.",
                        0xFF0000
                    )
            else:
                self.consecutive_losses = 0

            color = 0x3498DB if profit_pct > 0 else 0xE74C3C
            desc = (
                f"**📊 Ticker:** {symbol}\n"
                f"**🎯 Direction:** {trade['direction']} Spread\n"
                f"**🚪 Exit Price:** ${current_spread_value:.2f} per contract\n\n"
                f"**📈 Performance:**\n"
                f"• Net Profit: {profit_pct*100:+.2f}%\n"
                f"• Dollar PnL: ${dollar_pnl:+.2f}\n"
                f"• Max Profit Reached: {trade.get('max_profit_pct', 0)*100:.2f}%\n\n"
                f"**📝 Exit Reason:** {reason}"
            )
            self.send_discord_alert("🔵 CLOSED 0DTE SPREAD POSITION", desc, color)

            # Capture exit-time indicators for the audit log
            df = self.fetch_intraday_data(symbol)
            exit_indicators = trade.get('entry_indicators', {}).copy()
            # >= 30 bars: ADX(14) raises "index out of bounds" below ~29 bars, and an
            # exception here would abort the rest of close_position (incl. the audit row)
            if not df.empty and len(df) >= 30:
                vwap_ind = ta.volume.VolumeWeightedAveragePrice(
                    high=df['high'], low=df['low'], close=df['close'], volume=df['volume']
                )
                df.loc[:, 'VWAP'] = vwap_ind.volume_weighted_average_price()
                adx_ind = ta.trend.ADXIndicator(
                    high=df['high'], low=df['low'], close=df['close'], window=14
                )
                df.loc[:, 'ADX'] = adx_ind.adx()
                now = datetime.datetime.now(pytz.timezone('America/New_York'))
                market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
                orb_bars = df[(df.index >= market_open) & (df.index < market_open + datetime.timedelta(minutes=30))]
                if not orb_bars.empty:
                    exit_indicators = {
                        'adx': df['ADX'].iloc[-1], 'vwap': df['VWAP'].iloc[-1],
                        'orb_high': orb_bars['high'].max(), 'orb_low': orb_bars['low'].min(),
                        'current_price': df['close'].iloc[-1]
                    }

            self.log_audit(
                "SELL", symbol, trade['direction'], current_spread_value, reason,
                adx=exit_indicators.get('adx'), vwap=exit_indicators.get('vwap'),
                orb_high=exit_indicators.get('orb_high'), orb_low=exit_indicators.get('orb_low'),
                underlying_price=exit_indicators.get('current_price'),
                profit_pct=profit_pct, dollar_pnl=dollar_pnl,
                peak_pct=trade.get('max_profit_pct')
            )
        except Exception as e:
            logger.error(f"[{symbol}] Failed to submit IBKR closing order: {e}")
        finally:
            self.active_trades.pop(symbol, None)

    # ── Summaries & end-of-day ───────────────────────────────────────────────

    def send_today_summary(self):
        """Send a Discord snapshot of all of today's trades — open (live P&L),
        closed (realized P&L), and the running net. Reads cached values; no IBKR calls."""
        open_lines = []
        for sym, trade in self.active_trades.items():
            direction = trade.get('direction', '')
            if trade.get('status') == 'PENDING_ENTRY' or 'current_value' not in trade:
                open_lines.append(f"• {sym} {direction} — pending fill")
                continue
            entry = trade['entry_price']
            cur = trade['current_value']
            pct = trade.get('current_profit_pct', 0) * 100
            peak = trade.get('max_profit_pct', 0) * 100
            open_lines.append(
                f"• {sym} {direction}  ${entry:.2f} → ${cur:.2f}  {pct:+.1f}%  (peak {peak:+.1f}%)"
            )

        closed_lines = []
        net = 0.0
        for c in self.closed_trades_today:
            net += c['dollar_pnl']
            closed_lines.append(
                f"• {c['symbol']} {c['direction']}  {c['profit_pct']*100:+.1f}%  ${c['dollar_pnl']:+.2f}"
            )

        desc = f"**▶ OPEN ({len(self.active_trades)})**\n"
        desc += ("\n".join(open_lines) if open_lines else "_none_") + "\n\n"
        desc += f"**✅ CLOSED ({len(self.closed_trades_today)})**\n"
        desc += ("\n".join(closed_lines) if closed_lines else "_none_") + "\n\n"
        desc += f"**💵 Net so far (realized):** ${net:+.2f}"

        color = 0x2ECC71 if net >= 0 else 0xE74C3C
        self.send_discord_alert("📋 TODAY", desc, color)

    def close_all_positions(self, reason: str):
        """Force-close every open position (end-of-day flatten)."""
        for symbol in list(self.active_trades.keys()):
            trade = self.active_trades[symbol]
            try:
                if trade.get('status') == 'PENDING_ENTRY':
                    # Unfilled order lingering near the close — cancel instead of selling
                    self.ib.cancelOrder(trade['ibkr_trade'].order)
                    logger.info(f"[{symbol}] EOD: cancelled unfilled entry order.")
                    self.active_trades.pop(symbol, None)
                    continue
                current_spread_value = self.get_spread_value(
                    symbol, trade['direction'], trade['long_strike'], trade['short_strike']
                )
                if current_spread_value <= 0:
                    current_spread_value = 0.01  # still flatten — submit at minimum tick
                self.close_position(symbol, current_spread_value, reason)
            except Exception as e:
                logger.error(f"[{symbol}] EOD flatten failed: {e}")
                self.active_trades.pop(symbol, None)

    def send_day_summary(self):
        """Send the end-of-day realized P&L summary to Discord."""
        trades = self.closed_trades_today
        today = datetime.datetime.now(pytz.timezone('America/New_York')).date()
        if not trades:
            return

        net = sum(c['dollar_pnl'] for c in trades)
        wins = sum(1 for c in trades if c['profit_pct'] > 0)
        losses = sum(1 for c in trades if c['profit_pct'] <= 0)
        win_rate = (wins / len(trades) * 100) if trades else 0.0

        lines = [
            f"• {c['symbol']} {c['direction']}  {c['profit_pct']*100:+.1f}%  ${c['dollar_pnl']:+.2f}"
            for c in trades
        ]
        desc = (
            f"**📅 {today}**\n\n"
            f"**💵 Net P&L:** ${net:+.2f}\n"
            f"**📊 Trades:** {len(trades)}  |  Wins: {wins}  Losses: {losses}  "
            f"(Win rate: {win_rate:.0f}%)\n"
        )
        if self.circuit_breaker_tripped:
            desc += "**🚨 Circuit breaker tripped today**\n"
        desc += "\n**Trades:**\n" + "\n".join(lines)

        color = 0x2ECC71 if net >= 0 else 0xE74C3C
        self.send_discord_alert("📅 DAY SUMMARY", desc, color)

    def rebuild_dashboard(self):
        """Regenerate dashboard.xlsx from audit.csv (runs after the day summary).
        Runs as a subprocess so a dashboard failure can never break the bot loop."""
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'scripts', 'build_dashboard.py'
        )
        try:
            result = subprocess.run(
                [sys.executable, script],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                logger.info(f"Dashboard rebuilt: {result.stdout.strip()}")
            else:
                logger.warning(f"Dashboard rebuild failed: {result.stderr.strip()[-300:]}")
        except Exception as e:
            logger.warning(f"Dashboard rebuild failed: {e}")

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self):
        logger.info("Starting 0DTE Options Spread Trading Bot (IBKR)...")
        while True:
            try:
                self._ensure_connected()

                if not self.is_market_open():
                    # Market just closed for the day — send the day summary once,
                    # then refresh dashboard.xlsx with the day's trades
                    if self.daily_trade_count > 0 and not self.daily_summary_sent:
                        self.send_day_summary()
                        self.daily_summary_sent = True
                        self.rebuild_dashboard()
                    secs = self._seconds_until_market_open()
                    hrs, rem = divmod(secs, 3600)
                    mins = rem // 60
                    if hrs > 0:
                        logger.info(f"Market closed. Next open in {hrs}h {mins}m. Sleeping 1 hour.")
                        self.ib.sleep(3600)   # wake hourly to keep IBKR connection alive
                    else:
                        logger.info(f"Market opens in {mins}m {rem % 60}s. Sleeping until open.")
                        self.ib.sleep(secs)
                    continue

                self.check_and_reset_daily_trade_count()
                self.evaluate_exit_conditions()

                # End-of-day flatten — never hold 0DTE positions into the close
                if self._is_eod_flatten_time() and self.active_trades:
                    logger.info("EOD flatten time reached — closing all open positions.")
                    self.close_all_positions("End of day — flattening 0DTE positions")

                if not self._is_entry_window():
                    logger.info("Entry window closed after 3:00 PM EST; skipping new entries.")
                else:
                    for symbol in config.SYMBOLS:
                        if symbol not in self.active_trades:
                            direction, reason, indicators = self.evaluate_entry_strategy(symbol)
                            if direction in ("CALL", "PUT"):
                                self.execute_trade(symbol, direction, reason, indicators)

                self.ib.sleep(60)
            except KeyboardInterrupt:
                logger.info("Bot stopped manually.")
                self.ib.disconnect()
                break
            except Exception as e:
                logger.error(f"Unexpected error in main loop: {e}")
                self.ib.sleep(60)
