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
        # Breadth data cache — TICK and VOLD are market-wide, shared across all
        # symbols. Cache for 60 seconds to avoid 3 redundant IBKR requests per loop.
        self._breadth_cache_time: Optional[datetime.datetime] = None
        self._tick_df_cache: pd.DataFrame = pd.DataFrame()
        self._vold_df_cache: pd.DataFrame = pd.DataFrame()

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
                  dollar_pnl: Optional[float] = None, breadth: Optional[str] = None):
        file_exists = os.path.isfile("audit.csv")
        try:
            with open("audit.csv", mode='a', newline='') as file:
                writer = csv.writer(file)
                if not file_exists:
                    writer.writerow([
                        "Timestamp", "Action", "Symbol", "Direction", "Price", "Underlying_Price",
                        "ADX", "VWAP", "ORB_High", "ORB_Low", "Breadth", "Reason", "Profit_Pct", "Dollar_PnL"
                    ])
                writer.writerow([
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    action, symbol, direction, price,
                    f"{underlying_price:.2f}" if underlying_price else "",
                    f"{adx:.2f}" if adx else "",
                    f"{vwap:.2f}" if vwap else "",
                    f"{orb_high:.2f}" if orb_high else "",
                    f"{orb_low:.2f}" if orb_low else "",
                    breadth or "",
                    reason,
                    f"{profit_pct:.2f}%" if profit_pct is not None else "",
                    f"{dollar_pnl:.2f}" if dollar_pnl is not None else ""
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
        if df.empty or len(df) < 20:
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

            indicators = {
                'adx': current_adx, 'vwap': current_vwap,
                'orb_high': orb_high, 'orb_low': orb_low, 'current_price': current_close
            }

            if current_close > current_vwap and current_close > orb_high:
                direction = "CALL"
                reason = f"Bullish: Price ({current_close:.2f}) > VWAP ({current_vwap:.2f}) and ORB High ({orb_high:.2f}). ADX: {current_adx:.2f}"
            elif current_close < current_vwap and current_close < orb_low:
                direction = "PUT"
                reason = f"Bearish: Price ({current_close:.2f}) < VWAP ({current_vwap:.2f}) and ORB Low ({orb_low:.2f}). ADX: {current_adx:.2f}"
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

        except Exception as e:
            logger.error(f"Failed to place IBKR spread order for {symbol}: {e}")

    # ── Exit management ──────────────────────────────────────────────────────

    def evaluate_exit_conditions(self):
        for symbol in list(self.active_trades.keys()):
            self.evaluate_exit_conditions_for_symbol(symbol)

    def evaluate_exit_conditions_for_symbol(self, symbol: str):
        if symbol not in self.active_trades:
            return

        trade = self.active_trades[symbol]

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
                    self.log_audit(
                        "BUY", symbol, trade['direction'], filled_price, trade.get('reason', ''),
                        adx=trade['entry_indicators'].get('adx'),
                        vwap=trade['entry_indicators'].get('vwap'),
                        orb_high=trade['entry_indicators'].get('orb_high'),
                        orb_low=trade['entry_indicators'].get('orb_low'),
                        underlying_price=trade['entry_indicators'].get('current_price'),
                        breadth=trade['entry_indicators'].get('breadth')
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

        if profit_pct > trade['max_profit_pct']:
            trade['max_profit_pct'] = profit_pct
            if profit_pct > 0:
                logger.info(f"[{symbol}] New Max Profit: {profit_pct*100:.2f}%")

        exit_triggered = False
        exit_reason = ""

        # Two-rule exit model:
        #   1. Hard stop — cap the downside at HARD_STOP_LOSS_PCT (70%).
        #   2. Trailing stop — does NOTHING until the trade has peaked at
        #      TAKE_PROFIT_TRAIL_TRIGGER (50%). Below that the position is left
        #      to breathe (only the hard stop applies). Once it has been up 50%+,
        #      exit if profit falls to (1 - TRAILING_STOP_LOSS_PCT) of the peak —
        #      i.e. gives back 10% OF the peak (peak +50% -> exit +45%). Because
        #      this only arms at +50%, the threshold is always >= +45%, so it
        #      can never exit at a loss.

        # Rule 1: Hard stop loss
        if profit_pct <= -config.HARD_STOP_LOSS_PCT:
            exit_triggered = True
            exit_reason = f"Hard stop loss: spread lost {abs(profit_pct)*100:.1f}% of entry value"

        # Rule 2: Trailing stop, armed only after reaching +50% peak
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
            if not df.empty and len(df) >= 20:
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
                profit_pct=profit_pct, dollar_pnl=dollar_pnl
            )
        except Exception as e:
            logger.error(f"[{symbol}] Failed to submit IBKR closing order: {e}")
        finally:
            self.active_trades.pop(symbol, None)

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self):
        logger.info("Starting 0DTE Options Spread Trading Bot (IBKR)...")
        while True:
            try:
                self._ensure_connected()

                if not self.is_market_open():
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
