"""IBKRBroker — everything that talks to Interactive Brokers.

Connection lifecycle, error routing, contract construction, market data
(underlying price, intraday bars, option quotes, breadth indices), and order
placement. No strategy logic lives here.
"""
import datetime
import logging
from typing import Optional, Tuple

import pandas as pd
from ib_insync import IB, Stock, Option, Index, Contract, ComboLeg, LimitOrder, util

import config
import market_time

logger = logging.getLogger(__name__)


class IBKRBroker:
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

    def __init__(self):
        self.ib = IB()
        # Breadth data cache — TICK and VOLD are market-wide, shared across all
        # symbols. Cache for 60 seconds to avoid redundant IBKR requests per loop.
        self._breadth_cache_time: Optional[datetime.datetime] = None
        self._tick_df_cache: pd.DataFrame = pd.DataFrame()
        self._vold_df_cache: pd.DataFrame = pd.DataFrame()

    # ── Connection ───────────────────────────────────────────────────────────

    def connect(self):
        logger.info(f"Connecting to IBKR at {config.IBKR_HOST}:{config.IBKR_PORT} (clientId={config.IBKR_CLIENT_ID})")
        self.ib.connect(config.IBKR_HOST, config.IBKR_PORT, clientId=config.IBKR_CLIENT_ID)
        # Use delayed data (type 4) so paper accounts work without live subscriptions
        self.ib.reqMarketDataType(4)
        # Silence ib_insync's internal wrapper logger — it prints every IBKR error
        # code (including expected ones like 162 "no data yet") as ERROR. We route
        # real errors ourselves via errorEvent below.
        logging.getLogger('ib_insync.wrapper').setLevel(logging.CRITICAL)
        self.ib.errorEvent += self._on_ibkr_error
        # Subscribe to account positions so ib.positions() stays live — used to
        # reconcile tracked trades against the real account (detect manual closes).
        try:
            self.ib.reqPositions()
        except Exception as e:
            logger.warning(f"Could not subscribe to positions: {e}")
        logger.info("Connected to IBKR")

    def _on_ibkr_error(self, reqId: int, errorCode: int, errorString: str, contract):
        """Route IBKR error events: suppress expected codes, log real problems."""
        if errorCode in self._IBKR_INFO_CODES:
            logger.debug(f"IBKR [{errorCode}] reqId={reqId}: {errorString}")
        else:
            logger.warning(f"IBKR error [{errorCode}] reqId={reqId}: {errorString}")

    def ensure_connected(self):
        if not self.ib.isConnected():
            logger.warning("IBKR connection lost. Reconnecting...")
            try:
                self.connect()
            except Exception as e:
                logger.error(f"Reconnect failed: {e}")

    def sleep(self, seconds: float):
        """ib_insync sleep — keeps the IBKR event loop alive during the wait."""
        self.ib.sleep(seconds)

    def disconnect(self):
        self.ib.disconnect()

    # ── Contracts ────────────────────────────────────────────────────────────

    def underlying_contract(self, symbol: str):
        """Return the correct IBKR contract type for an underlying symbol."""
        if symbol == 'SPX':
            return Index('SPX', 'CBOE', 'USD')
        return Stock(symbol, 'SMART', 'USD')

    def option_symbol(self, symbol: str) -> str:
        """Map 0DTE underlying symbol to the correct IBKR option root (SPX → SPXW)."""
        return 'SPXW' if symbol == 'SPX' else symbol

    def get_option_contract(self, symbol: str, direction: str, strike: float) -> Option:
        today_str = market_time.now_et().strftime('%Y%m%d')
        right = 'C' if direction == 'CALL' else 'P'
        contract = Option(self.option_symbol(symbol), today_str, strike, right, 'SMART')
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError(f"Cannot qualify option: {self.option_symbol(symbol)} {today_str} {right} {strike}")
        return qualified[0]

    def make_bag(self, symbol: str, long_conid: int, short_conid: int) -> Contract:
        """Build a BAG combo contract (long leg BUY, short leg SELL)."""
        bag = Contract()
        bag.symbol = self.option_symbol(symbol)
        bag.secType = 'BAG'
        bag.currency = 'USD'
        bag.exchange = 'SMART'
        bag.comboLegs = [
            ComboLeg(conId=long_conid, ratio=1, action='BUY', exchange='SMART'),
            ComboLeg(conId=short_conid, ratio=1, action='SELL', exchange='SMART'),
        ]
        return bag

    # ── Orders / account ─────────────────────────────────────────────────────

    def place_limit(self, contract: Contract, action: str, qty: int, price: float):
        """Submit a limit order; returns the ib_insync Trade object."""
        return self.ib.placeOrder(contract, LimitOrder(action, qty, round(price, 2)))

    def modify_limit_price(self, ibkr_trade, price: float):
        """Amend an open limit order's price in place (same orderId, no cancel race)."""
        ibkr_trade.order.lmtPrice = round(price, 2)
        return self.ib.placeOrder(ibkr_trade.contract, ibkr_trade.order)

    def cancel_order(self, order):
        self.ib.cancelOrder(order)

    def order_commission(self, ibkr_trade) -> float:
        """Total commissions IBKR reported for a trade's fills (all legs).
        Returns 0.0 if reports haven't arrived yet."""
        total = 0.0
        try:
            for f in ibkr_trade.fills:
                if f.commissionReport and f.commissionReport.commission:
                    total += float(f.commissionReport.commission)
        except Exception:
            pass
        return total

    def positions(self):
        return self.ib.positions()

    # ── Market data ──────────────────────────────────────────────────────────

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

    def get_current_price(self, symbol: str) -> float:
        """Fetch latest price for an underlying symbol from IBKR."""
        try:
            contract = self.underlying_contract(symbol)
            self.ib.qualifyContracts(contract)
            ticker = self._request_snapshot(contract)
            return self._ticker_mid(ticker)
        except Exception as e:
            logger.error(f"Error fetching price for {symbol}: {e}")
            return 0.0

    def fetch_intraday_data(self, symbol: str) -> pd.DataFrame:
        """Fetch 1-minute bars for the current trading day from IBKR."""
        try:
            contract = self.underlying_contract(symbol)
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

            # .copy() avoids pandas chained-assignment FutureWarnings on the
            # column writes below
            df = util.df(bars).copy()
            df['date'] = pd.to_datetime(df['date'])
            if df['date'].dt.tz is None:
                df['date'] = df['date'].dt.tz_localize('America/New_York')
            else:
                df['date'] = df['date'].dt.tz_convert('America/New_York')
            df = df.set_index('date')

            # Keep only today's RTH bars (IBKR durationStr='1 D' can include
            # yesterday's bars; delayed data also backfills with NaN rows)
            now = market_time.now_et()
            df = df[df.index >= market_time.market_open_today()]

            # Drop rows where key price fields are NaN (delayed feed backfill artefacts)
            df = df.dropna(subset=['open', 'high', 'low', 'close'])

            # SPX MIDPOINT bars have no volume; set dummy volume=1 so VWAP math works
            if 'volume' not in df.columns or (df['volume'] == 0).all():
                df['volume'] = 1

            # Stale-feed detector: on 2026-07-01 an entry was evaluated on
            # indicator values identical to ~2h-old data. Flag it if it recurs.
            if not df.empty and market_time.is_market_open():
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
            long_c = self.get_option_contract(symbol, direction, long_strike)
            short_c = self.get_option_contract(symbol, direction, short_strike)
            long_mid = self._get_option_mid(long_c)
            short_mid = self._get_option_mid(short_c)
            if long_mid <= 0 or short_mid <= 0:
                logger.warning(f"Could not price {symbol} {direction} {long_strike}/{short_strike}: long_mid={long_mid} short_mid={short_mid}")
                return 0.0
            return max(long_mid - short_mid, 0.01)
        except Exception as e:
            logger.error(f"Error fetching spread value for {symbol}: {e}")
            return 0.0

    def fetch_breadth_data(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Fetch $TICK and $VOLD 1-min bars from IBKR, cached for 60 seconds.

        Returns (tick_df, vold_df). On any error returns two empty DataFrames so
        the caller can fail-open and not block the trade.
        """
        now = market_time.now_et()
        if (self._breadth_cache_time is not None and
                (now - self._breadth_cache_time).total_seconds() < 60):
            return self._tick_df_cache, self._vold_df_cache

        try:
            today_open = market_time.market_open_today()

            def _fetch_index(contract):
                bars = self.ib.reqHistoricalData(
                    contract, endDateTime='', durationStr='1 D',
                    barSizeSetting='1 min', whatToShow='TRADES',
                    useRTH=True, formatDate=1, timeout=30)
                if not bars:
                    return pd.DataFrame()
                df = util.df(bars).copy()
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
