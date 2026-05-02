import time
from typing import Dict, Optional
import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import TimeFrame
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
        
        # State Management
        self.active_trade_symbol: Optional[str] = None
        self.active_direction: Optional[str] = None # 'CALL' or 'PUT'
        self.entry_price: float = 0.0
        self.max_unrealized_profit_pct: float = 0.0
        self.qty: float = 0.0

    def log_audit(self, action: str, symbol: str, direction: str, price: float, reason: str):
        """Logs trade decisions to an audit.csv file."""
        file_exists = os.path.isfile("audit.csv")
        try:
            with open("audit.csv", mode='a', newline='') as file:
                writer = csv.writer(file)
                if not file_exists:
                    writer.writerow(["Timestamp", "Action", "Symbol", "Direction", "Price", "Reason"])
                writer.writerow([datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, symbol, direction, price, reason])
        except Exception as e:
            logger.error(f"Failed to write to audit log: {e}")

    def get_current_price(self, symbol: str) -> float:
        """Fetch the latest trade price for an underlying symbol."""
        try:
            latest_trade = self.api.get_latest_trade(symbol, feed='iex')
            return float(latest_trade.price)
        except Exception as e:
            logger.error(f"Error fetching price for {symbol}: {e}")
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

    def evaluate_entry_strategy(self, symbol: str) -> Tuple[Optional[str], str]:
        """
        Calculates VWAP, 30-min ORB, and ADX to determine trend direction.
        Returns ('CALL'/'PUT', reason) or (None, "") if no setup.
        """
        df = self.fetch_intraday_data(symbol)
        if df.empty or len(df) < 30:
            return None, "" # Not enough data (e.g. market just opened)

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
            start_time = df.index[0].replace(hour=9, minute=30, second=0, microsecond=0)
            orb_end_time = start_time + datetime.timedelta(minutes=30)
            
            orb_bars = df[(df.index >= start_time) & (df.index < orb_end_time)]
            if orb_bars.empty:
                return None
                
            orb_high = orb_bars['high'].max()
            orb_low = orb_bars['low'].min()

            # Strategy Evaluation
            current_close = df['close'].iloc[-1]
            current_vwap = df['VWAP'].iloc[-1]
            current_adx = df['ADX'].iloc[-1]

            # We want strong trend (ADX > 25)
            if pd.isna(current_adx) or current_adx < 25:
                return None, "" # Trend is not strong enough

            # Bullish: Price > VWAP and Price breaks above 30-min ORB High
            if current_close > current_vwap and current_close > orb_high:
                return "CALL", f"Bullish: Price ({current_close}) > VWAP ({current_vwap:.2f}) and ORB High ({orb_high:.2f}). ADX: {current_adx:.2f}"
            
            # Bearish: Price < VWAP and Price breaks below 30-min ORB Low
            if current_close < current_vwap and current_close < orb_low:
                return "PUT", f"Bearish: Price ({current_close}) < VWAP ({current_vwap:.2f}) and ORB Low ({orb_low:.2f}). ADX: {current_adx:.2f}"

        except Exception as e:
            logger.error(f"Error calculating strategy indicators: {e}")

        return None, ""

    def execute_trade(self, symbol: str, direction: str, reason: str):
        """Execute an ATM Option Debit Spread."""
        if self.active_trade_symbol is not None:
            logger.warning("Already in an active trade. Concurrency limit is 1.")
            return

        underlying_price = self.get_current_price(symbol)
        if underlying_price <= 0:
            return

        spread_cost = 100.0  # Simulated cost per spread
        qty_to_buy = int(config.MAX_POSITION_SIZE // spread_cost)
        
        if qty_to_buy < 1:
            logger.warning(f"Spread cost exceeds max position size of ${config.MAX_POSITION_SIZE}.")
            return
            
        total_investment = qty_to_buy * spread_cost
        
        try:
            self.active_trade_symbol = symbol
            self.active_direction = direction
            self.entry_price = spread_cost
            self.qty = qty_to_buy
            self.max_unrealized_profit_pct = 0.0
            logger.info(f"EXECUTED {direction} SPREAD: {qty_to_buy} contracts of {symbol} at ${spread_cost:.2f} (Total: ${total_investment})")
            logger.info(f"Underlying Entry Price: ${underlying_price:.2f}")
            self.log_audit("BUY", symbol, direction, spread_cost, reason)
        except Exception as e:
            logger.error(f"Failed to execute spread: {e}")

    def evaluate_exit_conditions(self):
        """Evaluates risk management rules and exits if necessary."""
        if not self.active_trade_symbol:
            return

        current_spread_value = self.entry_price # Placeholder for mockup

        profit_pct = (current_spread_value - self.entry_price) / self.entry_price
        
        if profit_pct > self.max_unrealized_profit_pct:
            self.max_unrealized_profit_pct = profit_pct
            if profit_pct > 0:
                logger.info(f"New Max Profit Reached: {self.max_unrealized_profit_pct*100:.2f}%")

        exit_triggered = False
        exit_reason = ""

        if profit_pct <= config.HARD_STOP_LOSS_PCT:
            exit_triggered = True
            exit_reason = f"Hard Stop Loss 50% Hit. (Current: {profit_pct*100:.2f}%)"

        elif self.max_unrealized_profit_pct > 0 and profit_pct <= (self.max_unrealized_profit_pct * config.MAX_PROFIT_EXIT_MULTIPLIER):
            exit_triggered = True
            exit_reason = f"Dropped to 70% of Max Profit. (Max: {self.max_unrealized_profit_pct*100:.2f}%, Current: {profit_pct*100:.2f}%)"

        elif self.max_unrealized_profit_pct >= config.TAKE_PROFIT_TRAIL_TRIGGER:
            trailing_stop_threshold = self.max_unrealized_profit_pct - config.TRAILING_STOP_LOSS_PCT
            if profit_pct <= trailing_stop_threshold:
                exit_triggered = True
                exit_reason = f"10% Trailing Stop Triggered. (Max: {self.max_unrealized_profit_pct*100:.2f}%, Threshold: {trailing_stop_threshold*100:.2f}%, Current: {profit_pct*100:.2f}%)"

        if exit_triggered:
            logger.info(f"EXIT TRIGGERED: {exit_reason}")
            self.close_position(current_spread_value, exit_reason)

    def close_position(self, current_spread_value: float, reason: str):
        """Closes the active spread position."""
        try:
            logger.info(f"CLOSED SPREAD POSITION: {self.active_trade_symbol} {self.active_direction} at ${current_spread_value:.2f}")
            self.log_audit("SELL", self.active_trade_symbol, self.active_direction, current_spread_value, reason)
            
            self.active_trade_symbol = None
            self.active_direction = None
            self.entry_price = 0.0
            self.max_unrealized_profit_pct = 0.0
            self.qty = 0.0
        except Exception as e:
            logger.error(f"Failed to close position: {e}")

    def run(self):
        """Main execution loop."""
        logger.info("Starting Options Spread Trading Bot...")
        while True:
            try:
                if self.active_trade_symbol:
                    self.evaluate_exit_conditions()
                else:
                    for symbol in config.SYMBOLS:
                        direction, reason = self.evaluate_entry_strategy(symbol)
                        if direction in ["CALL", "PUT"]:
                            self.execute_trade(symbol, direction, reason)
                            break
                time.sleep(60)
            except KeyboardInterrupt:
                logger.info("Bot Stopped Manually.")
                break
            except Exception as e:
                logger.error(f"Unexpected error in main loop: {e}")
                time.sleep(60)
