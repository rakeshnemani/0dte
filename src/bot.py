import time
from typing import Dict, Optional
import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import TimeFrame
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, OptionLatestQuoteRequest, OptionLatestTradeRequest
from alpaca.trading.enums import ContractType
import logging
import pandas as pd
import ta
import datetime
import pytz
import os
import csv
from typing import Tuple

import config

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class TradingBot:
    def __init__(self):
        # Initialize Alpaca API using config variables
        self.api = tradeapi.REST(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            config.ALPACA_BASE_URL,
            api_version='v2'
        )

        self.options_client = OptionHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
            use_basic_auth=False,
            raw_data=False,
        )
        
        # State Management - tracks one active trade per symbol
        # {symbol: {direction, entry_price, qty, max_profit_pct, long_strike, short_strike}}
        self.active_trades: Dict[str, dict] = {}
        
        # Daily trade limit tracking - resets at market open (9:30 AM EST)
        self.daily_trade_count: int = 0
        self.last_trade_date: datetime.date = None

    def log_audit(self, action: str, symbol: str, direction: str, price: float, reason: str, 
                  adx: Optional[float] = None, vwap: Optional[float] = None, 
                  orb_high: Optional[float] = None, orb_low: Optional[float] = None,
                  underlying_price: Optional[float] = None):
        """Logs trade decisions to an audit.csv file with full indicator context."""
        file_exists = os.path.isfile("audit.csv")
        try:
            with open("audit.csv", mode='a', newline='') as file:
                writer = csv.writer(file)
                if not file_exists:
                    writer.writerow([
                        "Timestamp", "Action", "Symbol", "Direction", "Price", "Underlying_Price",
                        "ADX", "VWAP", "ORB_High", "ORB_Low", "Reason"
                    ])
                writer.writerow([
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    action, symbol, direction, price, 
                    f"{underlying_price:.2f}" if underlying_price else "",
                    f"{adx:.2f}" if adx else "",
                    f"{vwap:.2f}" if vwap else "",
                    f"{orb_high:.2f}" if orb_high else "",
                    f"{orb_low:.2f}" if orb_low else "",
                    reason
                ])
        except Exception as e:
            logger.error(f"Failed to write to audit log: {e}")

    def check_and_reset_daily_trade_count(self) -> None:
        """Resets daily trade count at market open (9:30 AM EST)."""
        today = datetime.datetime.now(pytz.timezone('America/New_York')).date()
        if self.last_trade_date != today:
            self.daily_trade_count = 0
            self.last_trade_date = today
            logger.info(f"Daily trade count reset for {today}")

    def get_current_price(self, symbol: str) -> float:
        """Fetch the latest trade price for an underlying symbol."""
        try:
            latest_trade = self.api.get_latest_trade(symbol, feed='iex')
            return float(latest_trade.price)
        except Exception as e:
            logger.error(f"Error fetching price for {symbol}: {e}")
            return 0.0

    def _format_occ_symbol(self, symbol: str, expiration_date: str, direction: str, strike: float) -> str:
        expiration_code = datetime.datetime.strptime(expiration_date, "%Y-%m-%d").strftime("%y%m%d")
        option_letter = "C" if direction == "CALL" else "P"
        strike_code = f"{int(round(strike * 1000)):08d}"
        return f"{symbol.upper()}{expiration_code}{option_letter}{strike_code}"

    def _quote_mid(self, quote) -> float:
        if not quote:
            return 0.0
        bid_price = float(quote.bid_price) if quote.bid_price is not None else 0.0
        ask_price = float(quote.ask_price) if quote.ask_price is not None else 0.0
        if bid_price > 0 and ask_price > 0:
            return (bid_price + ask_price) / 2
        if bid_price > 0:
            return bid_price
        if ask_price > 0:
            return ask_price
        return 0.0

    def _trade_price(self, trade) -> float:
        if not trade or trade.price is None:
            return 0.0
        return float(trade.price)

    def get_spread_value(self, symbol: str, direction: str, long_strike: float, short_strike: float) -> float:
        """Fetch the current market value of the option spread from Alpaca's options data API."""
        try:
            today = datetime.datetime.now(pytz.timezone('America/New_York')).date()
            expiration_date = today.isoformat()
            option_type = ContractType.CALL if direction == 'CALL' else ContractType.PUT

            def fetch_leg_price(strike: float) -> float:
                # First try to load the option chain for the exact strike.
                chain_request = OptionChainRequest(
                    underlying_symbol=symbol,
                    type=option_type,
                    expiration_date=expiration_date,
                    strike_price_gte=strike,
                    strike_price_lte=strike,
                )
                option_chain = self.options_client.get_option_chain(chain_request)
                if option_chain:
                    snapshot = next(iter(option_chain.values()), None)
                    if snapshot is not None:
                        mid = self._quote_mid(getattr(snapshot, 'latest_quote', None))
                        if mid > 0:
                            return mid
                        mid = self._trade_price(getattr(snapshot, 'latest_trade', None))
                        if mid > 0:
                            return mid

                # Fallback: use direct latest quote/trade on the specific OCC contract symbol.
                occ_symbol = self._format_occ_symbol(symbol, expiration_date, direction, strike)
                logger.info(f"Falling back to latest option quote for {occ_symbol}")

                quote_request = OptionLatestQuoteRequest(symbol_or_symbols=occ_symbol)
                latest_quote = self.options_client.get_option_latest_quote(quote_request)
                if latest_quote and occ_symbol in latest_quote:
                    mid = self._quote_mid(latest_quote[occ_symbol])
                    if mid > 0:
                        return mid

                trade_request = OptionLatestTradeRequest(symbol_or_symbols=occ_symbol)
                latest_trade = self.options_client.get_option_latest_trade(trade_request)
                if latest_trade and occ_symbol in latest_trade:
                    mid = self._trade_price(latest_trade[occ_symbol])
                    if mid > 0:
                        return mid

                return 0.0

            long_mid = fetch_leg_price(long_strike)
            short_mid = fetch_leg_price(short_strike)

            if long_mid <= 0 or short_mid <= 0:
                logger.warning(
                    f"Could not find option contracts for {symbol} {direction} strikes {long_strike}/{short_strike}"
                )
                return 0.0

            return max(long_mid - short_mid, 0.01)

        except Exception as e:
            logger.error(f"Error fetching spread value for {symbol}: {e}")
            return 0.0

    def fetch_intraday_data(self, symbol: str) -> pd.DataFrame:
        """Fetch 1-minute bar data for the current day to calculate indicators."""
        try:
            now = datetime.datetime.now(pytz.timezone('America/New_York'))
            start_of_day = now.replace(hour=9, minute=30, second=0, microsecond=0)
            
            bars = self.api.get_bars(
                symbol,
                TimeFrame.Minute,
                start=start_of_day.isoformat(),
                end=now.isoformat(),
                limit=1000,
                feed='iex'
            ).df
            
            # Ensure index is datetime
            if not bars.empty:
                bars.index = pd.to_datetime(bars.index).tz_convert('America/New_York')
            return bars
        except Exception as e:
            logger.error(f"Failed to fetch intraday data: {e}")
            return pd.DataFrame()

    def evaluate_entry_strategy(self, symbol: str) -> Tuple[Optional[str], str, dict]:
        """
        Calculates VWAP, 30-min ORB, and ADX to determine trend direction.
        Returns (direction, reason, indicators_dict) where direction is 'CALL'/'PUT' or None.
        indicators_dict contains: {adx, vwap, orb_high, orb_low, current_price}
        """
        # Time filter: Do not enter after 1:00 PM EST (14:00 in 24-hour format)
        now = datetime.datetime.now(pytz.timezone('America/New_York'))
        if now.hour >= 14:  # 2:00 PM or later
            return None, "", {}  # No entries after 1:00 PM to avoid late-day theta decay
        
        df = self.fetch_intraday_data(symbol)
        if df.empty or len(df) < 30:
            return None, "", {}  # Not enough data (e.g. market just opened)

        try:
            # 1. Calculate VWAP
            vwap_indicator = ta.volume.VolumeWeightedAveragePrice(
                high=df['high'], low=df['low'], close=df['close'], volume=df['volume']
            )
            df['VWAP'] = vwap_indicator.volume_weighted_average_price()
            
            # 2. Calculate ADX (Trend Strength)
            adx_indicator = ta.trend.ADXIndicator(
                high=df['high'], low=df['low'], close=df['close'], window=14
            )
            df['ADX'] = adx_indicator.adx()

            # 3. Calculate 30-Minute ORB (Opening Range Breakout)
            # Anchor to fixed market open time (9:30 AM EST), not df.index[0]
            # This ensures correct ORB calculation even if bot restarts mid-day
            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            orb_end_time = market_open + datetime.timedelta(minutes=30)
            
            orb_bars = df[(df.index >= market_open) & (df.index < orb_end_time)]
            if orb_bars.empty:
                return None, "", {}
                
            orb_high = orb_bars['high'].max()
            orb_low = orb_bars['low'].min()

            # Strategy Evaluation
            current_close = df['close'].iloc[-1]
            current_vwap = df['VWAP'].iloc[-1]
            current_adx = df['ADX'].iloc[-1]

            # We want strong trend (ADX > 25)
            if pd.isna(current_adx) or current_adx < 25:
                return None, "", {}  # Trend is not strong enough

            # Store indicators for logging
            indicators = {
                'adx': current_adx,
                'vwap': current_vwap,
                'orb_high': orb_high,
                'orb_low': orb_low,
                'current_price': current_close
            }

            # Bullish: Price > VWAP and Price breaks above 30-min ORB High
            if current_close > current_vwap and current_close > orb_high:
                reason = f"Bullish: Price ({current_close:.2f}) > VWAP ({current_vwap:.2f}) and ORB High ({orb_high:.2f}). ADX: {current_adx:.2f}"
                return "CALL", reason, indicators
            
            # Bearish: Price < VWAP and Price breaks below 30-min ORB Low
            if current_close < current_vwap and current_close < orb_low:
                reason = f"Bearish: Price ({current_close:.2f}) < VWAP ({current_vwap:.2f}) and ORB Low ({orb_low:.2f}). ADX: {current_adx:.2f}"
                return "PUT", reason, indicators

        except Exception as e:
            logger.error(f"Error calculating strategy indicators: {e}")

        return None, "", {}

    def execute_trade(self, symbol: str, direction: str, reason: str, indicators: dict):
        """Execute an ATM Option Debit Spread."""
        if symbol in self.active_trades:
            logger.warning(f"Already in an active trade for {symbol}. Skipping.")
            return

        # Check daily trade limit
        self.check_and_reset_daily_trade_count()
        if self.daily_trade_count >= config.MAX_TRADES_PER_DAY:
            logger.warning(f"Daily trade limit of {config.MAX_TRADES_PER_DAY} reached. No new entries.")
            return

        underlying_price = self.get_current_price(symbol)
        if underlying_price <= 0:
            return

        # Calculate ATM and OTM strikes
        atm_strike = round(underlying_price)
        strike_width = 1  # $1 wide spread
        
        if direction == "CALL":
            long_strike = atm_strike
            short_strike = atm_strike + strike_width
        else:  # PUT
            long_strike = atm_strike
            short_strike = atm_strike - strike_width
        
        # Fetch actual spread cost from Alpaca API
        spread_cost = self.get_spread_value(symbol, direction, long_strike, short_strike)
        if spread_cost <= 0:
            logger.warning(f"Could not fetch valid spread cost for {symbol}. Aborting trade.")
            return
        
        qty_to_buy = int(config.MAX_POSITION_SIZE // spread_cost)
        
        if qty_to_buy < 1:
            logger.warning(f"Spread cost ${spread_cost:.2f} exceeds max position size of ${config.MAX_POSITION_SIZE}.")
            return
            
        total_investment = qty_to_buy * spread_cost
        
        try:
            self.active_trades[symbol] = {
                'direction': direction,
                'entry_price': spread_cost,
                'qty': qty_to_buy,
                'max_profit_pct': 0.0,
                'long_strike': long_strike,
                'short_strike': short_strike,
                'entry_indicators': indicators  # Store indicators for potential exit logging
            }
            self.daily_trade_count += 1
            logger.info(f"EXECUTED {direction} SPREAD: {qty_to_buy} contracts of {symbol} at ${spread_cost:.2f} (Total: ${total_investment})")
            logger.info(f"Underlying Entry Price: ${underlying_price:.2f} | Long Strike: ${long_strike} | Short Strike: ${short_strike}")
            logger.info(f"Entry Indicators - ADX: {indicators.get('adx', 'N/A'):.2f if indicators.get('adx') else 'N/A'}, VWAP: {indicators.get('vwap', 'N/A'):.2f if indicators.get('vwap') else 'N/A'}, ORB: ({indicators.get('orb_low', 'N/A'):.2f if indicators.get('orb_low') else 'N/A'}, {indicators.get('orb_high', 'N/A'):.2f if indicators.get('orb_high') else 'N/A'})")
            logger.info(f"Daily trades: {self.daily_trade_count}/{config.MAX_TRADES_PER_DAY}")
            
            # Log to audit with full indicator context
            self.log_audit(
                "BUY", symbol, direction, spread_cost, reason,
                adx=indicators.get('adx'),
                vwap=indicators.get('vwap'),
                orb_high=indicators.get('orb_high'),
                orb_low=indicators.get('orb_low'),
                underlying_price=underlying_price
            )
        except Exception as e:
            logger.error(f"Failed to execute spread: {e}")

    def evaluate_exit_conditions(self):
        """Evaluates risk management rules for all active trades."""
        for symbol in list(self.active_trades.keys()):
            self.evaluate_exit_conditions_for_symbol(symbol)

    def evaluate_exit_conditions_for_symbol(self, symbol: str):
        """Evaluates risk management rules and exits if necessary for a specific symbol."""
        if symbol not in self.active_trades:
            return

        trade = self.active_trades[symbol]
        
        # Fetch current spread value from Alpaca API
        current_spread_value = self.get_spread_value(
            symbol, 
            trade['direction'], 
            trade['long_strike'], 
            trade['short_strike']
        )
        
        if current_spread_value <= 0:
            logger.warning(f"Could not fetch current spread value for {symbol}. Skipping exit evaluation.")
            return

        profit_pct = (current_spread_value - trade['entry_price']) / trade['entry_price']
        
        if profit_pct > trade['max_profit_pct']:
            trade['max_profit_pct'] = profit_pct
            if profit_pct > 0:
                logger.info(f"[{symbol}] New Max Profit Reached: {trade['max_profit_pct']*100:.2f}%")

        exit_triggered = False
        exit_reason = ""

        if profit_pct <= config.HARD_STOP_LOSS_PCT:
            exit_triggered = True
            exit_reason = f"Hard Stop Loss 50% Hit. (Current: {profit_pct*100:.2f}%)"

        elif trade['max_profit_pct'] > 0 and profit_pct <= (trade['max_profit_pct'] * config.MAX_PROFIT_EXIT_MULTIPLIER):
            exit_triggered = True
            exit_reason = f"Dropped to 70% of Max Profit. (Max: {trade['max_profit_pct']*100:.2f}%, Current: {profit_pct*100:.2f}%)"

        elif trade['max_profit_pct'] >= config.TAKE_PROFIT_TRAIL_TRIGGER:
            trailing_stop_threshold = trade['max_profit_pct'] - config.TRAILING_STOP_LOSS_PCT
            if profit_pct <= trailing_stop_threshold:
                exit_triggered = True
                exit_reason = f"10% Trailing Stop Triggered. (Max: {trade['max_profit_pct']*100:.2f}%, Threshold: {trailing_stop_threshold*100:.2f}%, Current: {profit_pct*100:.2f}%)"

        if exit_triggered:
            logger.info(f"[{symbol}] EXIT TRIGGERED: {exit_reason}")
            self.close_position(symbol, current_spread_value, exit_reason)

    def close_position(self, symbol: str, current_spread_value: float, reason: str):
        """Closes the active spread position for a symbol."""
        if symbol not in self.active_trades:
            return
        
        trade = self.active_trades[symbol]
        try:
            # Fetch current indicators at exit time for logging context
            df = self.fetch_intraday_data(symbol)
            exit_indicators = {}
            if not df.empty:
                vwap_indicator = ta.volume.VolumeWeightedAveragePrice(
                    high=df['high'], low=df['low'], close=df['close'], volume=df['volume']
                )
                df['VWAP'] = vwap_indicator.volume_weighted_average_price()
                
                adx_indicator = ta.trend.ADXIndicator(
                    high=df['high'], low=df['low'], close=df['close'], window=14
                )
                df['ADX'] = adx_indicator.adx()
                
                now = datetime.datetime.now(pytz.timezone('America/New_York'))
                market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
                orb_end_time = market_open + datetime.timedelta(minutes=30)
                orb_bars = df[(df.index >= market_open) & (df.index < orb_end_time)]
                
                if not orb_bars.empty:
                    exit_indicators = {
                        'adx': df['ADX'].iloc[-1],
                        'vwap': df['VWAP'].iloc[-1],
                        'orb_high': orb_bars['high'].max(),
                        'orb_low': orb_bars['low'].min(),
                        'current_price': df['close'].iloc[-1]
                    }
            
            logger.info(f"[{symbol}] CLOSED SPREAD POSITION: {trade['direction']} at ${current_spread_value:.2f}")
            self.log_audit(
                "SELL", symbol, trade['direction'], current_spread_value, reason,
                adx=exit_indicators.get('adx'),
                vwap=exit_indicators.get('vwap'),
                orb_high=exit_indicators.get('orb_high'),
                orb_low=exit_indicators.get('orb_low'),
                underlying_price=exit_indicators.get('current_price')
            )
            
            del self.active_trades[symbol]
        except Exception as e:
            logger.error(f"Failed to close position for {symbol}: {e}")

    def run(self):
        """Main execution loop."""
        logger.info("Starting Options Spread Trading Bot...")
        while True:
            try:
                # Evaluate exits for all active trades
                self.evaluate_exit_conditions()
                
                # Look for new entries in symbols without active trades
                for symbol in config.SYMBOLS:
                    if symbol not in self.active_trades:
                        direction, reason, indicators = self.evaluate_entry_strategy(symbol)
                        if direction in ["CALL", "PUT"]:
                            self.execute_trade(symbol, direction, reason, indicators)
                
                time.sleep(60)
            except KeyboardInterrupt:
                logger.info("Bot Stopped Manually.")
                break
            except Exception as e:
                logger.error(f"Unexpected error in main loop: {e}")
                time.sleep(60)
