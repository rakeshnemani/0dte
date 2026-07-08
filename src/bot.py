"""TradingBot — orchestration only.

Owns the trade state and the main loop; delegates everything else:
    broker.py       IBKR connection, market data, orders, positions
    strategy.py     entry signal, conviction score, exit rules (pure functions)
    notifier.py     Discord alerts and summaries
    audit.py        audit.csv writer
    market_time.py  ET market-hours helpers
"""
import datetime
import logging
import os
import subprocess
import sys
from typing import Dict

import audit
import config
import market_time
import notifier
import strategy
from broker import IBKRBroker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class TradingBot:
    def __init__(self):
        self.broker = IBKRBroker()
        self.broker.connect()

        # State management — one active trade per symbol
        self.active_trades: Dict[str, dict] = {}
        self.daily_trade_count: int = 0
        self.last_trade_date: datetime.date = None
        # Cooldown: maps (symbol, direction) → datetime when the cooldown expires,
        # so the same signal can re-trigger after SIGNAL_COOLDOWN_MINUTES.
        self.signal_cooldowns: Dict[tuple, datetime.datetime] = {}
        self.consecutive_losses: int = 0
        self.circuit_breaker_tripped: bool = False
        self.daily_loss_limit_hit: bool = False
        # Day-level P&L tracking — populated as trades close, used for the
        # post-close day summary.
        self.closed_trades_today: list = []
        self.daily_summary_sent: bool = False
        # Chop tracking: invalidation exits per (symbol, direction) today —
        # feeds the entry throttle and the conviction-score penalty.
        self.invalidation_counts: Dict[tuple, int] = {}
        # Last raw directional lean per symbol (price vs VWAP), for the
        # cross-symbol agreement component of the conviction score.
        self.symbol_lean: Dict[str, tuple] = {}
        # Entry scans stay on a ~60s cadence even when the loop fast-polls exits
        self._last_entry_scan = None
        self._last_interval: int = 60

        # A restart wipes active_trades — adopt any open option positions the
        # account still holds so they are managed instead of orphaned.
        self.adopt_orphan_positions()

    # ── Daily reset ──────────────────────────────────────────────────────────

    def check_and_reset_daily_trade_count(self) -> None:
        today = market_time.now_et().date()
        if self.last_trade_date != today:
            self.daily_trade_count = 0
            self.last_trade_date = today
            self.signal_cooldowns.clear()
            self.consecutive_losses = 0
            self.circuit_breaker_tripped = False
            self.daily_loss_limit_hit = False
            self.closed_trades_today = []
            self.daily_summary_sent = False
            self.invalidation_counts.clear()
            self.symbol_lean.clear()
            logger.info(f"Daily trade count reset for {today}")

    def _realized_pnl_today(self) -> float:
        """Realized P&L for the day, net of commissions (confirmed fills only)."""
        return sum(c['dollar_pnl'] - c.get('commission', 0.0)
                   for c in self.closed_trades_today)

    # ── Startup adoption / reconciliation ────────────────────────────────────

    def adopt_orphan_positions(self):
        """Adopt open option spreads the account holds but the bot isn't
        tracking (orphaned by a restart). Adopted trades become ACTIVE and are
        managed by the normal exit rules and EOD flatten. Positions that can't
        be paired into a spread (or aren't 0DTE) are alerted, not adopted."""
        try:
            positions = self.broker.positions()
        except Exception as e:
            logger.warning(f"Startup position scan failed: {e}")
            return

        today_str = market_time.now_et().strftime('%Y%m%d')
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

            bag = self.broker.make_bag(underlying, long_p.contract.conId, short_p.contract.conId)
            self.active_trades[underlying] = {
                'direction': direction,
                'target_entry_price': entry_price,
                'entry_price': entry_price,
                'status': 'ACTIVE',
                'activated_at': market_time.now_et(),
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
            notifier.notify_adopted(adopted)
        if leftovers:
            lines = [f"• {p.contract.localSymbol or p.contract.symbol}  pos {p.position:+.0f}"
                     for p in leftovers]
            logger.warning(f"Unadoptable positions found at startup: {len(leftovers)}")
            notifier.notify_unadoptable(lines)

    def _position_still_open(self, trade: dict) -> bool:
        """True if the spread's long leg is still held in the IBKR account.
        Fails open whenever the answer can't be determined, so a data hiccup
        never causes the bot to abandon a real open position."""
        long_conid = trade.get('long_conid')
        if long_conid is None:
            return True  # nothing to match against — assume open

        # Grace period: give the account feed time to reflect a just-filled
        # entry before this leg could be (wrongly) reported as missing.
        activated_at = trade.get('activated_at')
        if activated_at is not None:
            if (market_time.now_et() - activated_at).total_seconds() < 90:
                return True

        try:
            positions = self.broker.positions()
        except Exception as e:
            logger.warning(f"Position reconciliation fetch failed: {e}")
            return True  # fail-open
        for p in positions:
            if p.contract.conId == long_conid and p.position != 0:
                return True
        return False

    # ── Entry ────────────────────────────────────────────────────────────────

    def evaluate_entry_strategy(self, symbol: str):
        """Fetch bars, run the strategy signal, annotate breadth + conviction.
        Returns (direction, reason, indicators)."""
        now = market_time.now_et()
        df = self.broker.fetch_intraday_data(symbol)
        # ADX(14) in the ta library needs ~2×window+1 bars — with fewer it raises
        # "index 14 is out of bounds" (seen every morning ~09:50–09:58 ET).
        if df.empty or len(df) < 30:
            return None, "", {}

        direction, reason, indicators, lean = strategy.entry_signal(symbol, df, now)

        # Record this symbol's raw lean every loop — other symbols' conviction
        # scores read it for the cross-symbol agreement component.
        if lean is not None:
            self.symbol_lean[symbol] = (lean, now)

        if direction is None:
            return None, "", {}

        # Breadth annotation (logged, not a gate — fails open on missing data)
        tick_df, vold_df = self.broker.fetch_breadth_data()
        confirmed, breadth_reason = strategy.breadth_confirms(direction, tick_df, vold_df)
        breadth_label = "✓ confirmed" if confirmed else "✗ diverging"
        indicators['breadth'] = f"{breadth_label} | {breadth_reason}"
        logger.info(f"[{symbol}] Breadth ({direction}): {breadth_label} — {breadth_reason}")

        # Conviction score — sizes the position in execute_trade
        conviction = strategy.conviction_score(
            symbol, direction, df, indicators, now,
            self.symbol_lean, sum(self.invalidation_counts.values())
        )
        indicators['conviction'] = conviction
        indicators['conviction_str'] = conviction['str']
        logger.info(f"[{symbol}] Conviction: {conviction['str']}")

        return direction, reason, indicators

    def execute_trade(self, symbol: str, direction: str, reason: str, indicators: dict):
        """Run the entry guards, size by conviction, and submit the BAG order."""
        if symbol in self.active_trades:
            logger.warning(f"Already in active trade for {symbol}. Skipping.")
            return

        now_est = market_time.now_et()
        cooldown_expires = self.signal_cooldowns.get((symbol, direction))
        if cooldown_expires and now_est < cooldown_expires:
            remaining = int((cooldown_expires - now_est).total_seconds() // 60)
            logger.info(f"Signal ({symbol}, {direction}) in cooldown for {remaining}m more. Skipping.")
            return

        # Invalidation throttle: this signal has been proven chop today
        inv_count = self.invalidation_counts.get((symbol, direction), 0)
        if config.MAX_INVALIDATIONS_PER_SIGNAL > 0 and inv_count >= config.MAX_INVALIDATIONS_PER_SIGNAL:
            logger.info(
                f"Signal ({symbol}, {direction}) stood down: {inv_count} thesis-invalidation "
                f"exits today (limit {config.MAX_INVALIDATIONS_PER_SIGNAL}). No re-entry until tomorrow."
            )
            return

        if self.circuit_breaker_tripped:
            logger.warning(f"Circuit breaker active — {self.consecutive_losses} consecutive losses. No new entries today.")
            return

        if self.daily_loss_limit_hit:
            logger.warning(
                f"Daily loss limit active — realized ${self._realized_pnl_today():+.2f} "
                f"(limit -${config.MAX_DAILY_LOSS:.0f}). No new entries today."
            )
            return

        if self.daily_trade_count >= config.MAX_TRADES_PER_DAY:
            logger.warning(f"Daily trade limit of {config.MAX_TRADES_PER_DAY} reached.")
            return

        underlying_price = self.broker.get_current_price(symbol)
        if underlying_price <= 0:
            return

        step = config.STRIKE_STEP.get(symbol, 1)
        atm_strike = round(underlying_price / step) * step
        strike_width = config.SPREAD_WIDTH.get(symbol, 1)

        if direction == "CALL":
            long_strike, short_strike = atm_strike, atm_strike + strike_width
        else:
            long_strike, short_strike = atm_strike, atm_strike - strike_width

        spread_cost = self.broker.get_spread_value(symbol, direction, long_strike, short_strike)
        if spread_cost <= 0:
            logger.warning(f"Could not fetch valid spread cost for {symbol}. Aborting.")
            return

        if spread_cost < config.MIN_SPREAD_COST:
            logger.warning(f"Spread cost ${spread_cost:.2f} below minimum ${config.MIN_SPREAD_COST:.2f}. Skipping.")
            return

        # Conviction-based sizing: LOW 0.5x, MEDIUM 1x, HIGH 1.5x of base budget
        conviction = indicators.get('conviction')
        budget = config.MAX_POSITION_SIZE
        if config.CONVICTION_SIZING_ENABLED and conviction:
            budget = config.MAX_POSITION_SIZE * conviction['mult']
            logger.info(f"[{symbol}] Conviction {conviction['tier']} ({conviction['score']}/5) → budget ${budget:.0f}")

        qty_to_buy = int(budget // (spread_cost * 100))
        if qty_to_buy < 1:
            logger.warning(f"Spread cost ${spread_cost:.2f}/share exceeds position budget ${budget:.0f}.")
            return

        try:
            long_c = self.broker.get_option_contract(symbol, direction, long_strike)
            short_c = self.broker.get_option_contract(symbol, direction, short_strike)
            bag = self.broker.make_bag(symbol, long_c.conId, short_c.conId)
            ibkr_trade = self.broker.place_limit(bag, 'BUY', qty_to_buy, spread_cost)

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

            # Alert immediately on submission (fires even if later rejected),
            # then the consolidated "today" snapshot.
            notifier.notify_submit(symbol, direction, long_strike, short_strike,
                                   spread_cost, qty_to_buy, budget, indicators,
                                   reason, ibkr_trade.order.orderId)
            notifier.notify_today_summary(self.active_trades, self.closed_trades_today)

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

        # A closing order is in flight — confirm its fill before anything else
        if trade.get('status') == 'PENDING_EXIT':
            self._check_pending_exit(symbol, trade)
            return

        # Reconcile against the real IBKR account. If an ACTIVE position is no
        # longer held (closed manually via Client Portal / mobile / TWS,
        # assigned, etc.), stop tracking it instead of trying to manage or sell
        # a position we don't own. Require two consecutive "missing" reads so a
        # transient empty snapshot can't drop a live trade.
        if trade.get('status') == 'ACTIVE':
            if self._position_still_open(trade):
                trade['reconcile_misses'] = 0
            else:
                trade['reconcile_misses'] = trade.get('reconcile_misses', 0) + 1
                if trade['reconcile_misses'] >= 2:
                    logger.warning(f"[{symbol}] Position no longer in IBKR account — closed externally. Dropping from tracking.")
                    notifier.notify_closed_externally(symbol, trade['direction'])
                    self.active_trades.pop(symbol, None)
                else:
                    logger.info(f"[{symbol}] Position not found in account (miss {trade['reconcile_misses']}/2); re-checking next loop.")
                return  # skip exit eval while the position's status is uncertain/closed

        current_spread_value = self.broker.get_spread_value(
            symbol, trade['direction'], trade['long_strike'], trade['short_strike']
        )
        if current_spread_value <= 0:
            logger.warning(f"Could not fetch spread value for {symbol}. Skipping exit eval.")
            return

        if trade.get('status') == 'PENDING_ENTRY':
            self._check_pending_fill(symbol, trade)
            return

        profit_pct = (current_spread_value - trade['entry_price']) / trade['entry_price']

        # Cache live P&L so the "today" summary can read it without extra IBKR calls
        trade['current_value'] = current_spread_value
        trade['current_profit_pct'] = profit_pct

        if profit_pct > trade['max_profit_pct']:
            trade['max_profit_pct'] = profit_pct
            if profit_pct > 0:
                logger.info(f"[{symbol}] New Max Profit: {profit_pct*100:.2f}%")

        # Thesis-invalidation check (sustained VWAP recross against the trade).
        # 1-min bars change at most once a minute, so cache the verdict ~50s —
        # fast polling shouldn't multiply bar fetches.
        invalidated = trade.get('_inval_last', False)
        if config.VWAP_INVALIDATION_BARS > 0:
            checked_at = trade.get('_inval_checked_at')
            if checked_at is None or (market_time.now_et() - checked_at).total_seconds() >= 50:
                try:
                    df = self.broker.fetch_intraday_data(symbol)
                    invalidated = strategy.thesis_invalidated(trade['direction'], df)
                except Exception as e:
                    logger.warning(f"[{symbol}] VWAP invalidation check failed: {e}")
                trade['_inval_checked_at'] = market_time.now_et()
                trade['_inval_last'] = invalidated

        exit_triggered, exit_reason = strategy.exit_decision(
            profit_pct, trade['max_profit_pct'], invalidated
        )
        if exit_triggered:
            logger.info(f"[{symbol}] EXIT TRIGGERED: {exit_reason}")
            self.close_position(symbol, current_spread_value, exit_reason)

    def _check_pending_fill(self, symbol: str, trade: dict):
        """Poll a PENDING_ENTRY order: promote to ACTIVE on fill, drop on cancel."""
        try:
            self.broker.sleep(0)  # flush event loop so orderStatus is current
            ibkr_trade = trade['ibkr_trade']
            status = ibkr_trade.orderStatus.status

            if status == 'Filled':
                filled_price = float(ibkr_trade.orderStatus.avgFillPrice)
                logger.info(f"[{symbol}] IBKR ORDER FILLED at ${filled_price:.2f}")
                trade['status'] = 'ACTIVE'
                trade['entry_price'] = filled_price
                # Fill time — reconciliation grace period anchor
                trade['activated_at'] = market_time.now_et()
                ind = trade['entry_indicators']
                entry_commission = self.broker.order_commission(ibkr_trade)
                audit.record(
                    "BUY", symbol, trade['direction'], filled_price, trade.get('reason', ''),
                    adx=ind.get('adx'), vwap=ind.get('vwap'),
                    orb_high=ind.get('orb_high'), orb_low=ind.get('orb_low'),
                    underlying_price=ind.get('current_price'),
                    breadth=ind.get('breadth'),
                    adx_slope=ind.get('adx_slope'),
                    conviction=ind.get('conviction_str'),
                    commission=entry_commission,
                )
                notifier.notify_filled(symbol, trade, filled_price)

            elif status in ('Cancelled', 'ApiCancelled', 'Inactive'):
                logger.warning(f"[{symbol}] IBKR order {status}. Removing from tracking.")
                self.active_trades.pop(symbol, None)
            else:
                logger.info(f"[{symbol}] IBKR order still pending (Status: {status}).")
        except Exception as e:
            logger.error(f"[{symbol}] Error checking IBKR order status: {e}")

    # Reprice an unfilled closing order after this many seconds
    _EXIT_REPRICE_AFTER_S = 180

    def close_position(self, symbol: str, current_spread_value: float, reason: str):
        """Submit a closing BAG order and mark the trade PENDING_EXIT.

        P&L is NOT booked here — it's booked in _check_pending_exit once IBKR
        confirms the fill, using the actual fill price and reported commissions
        (the audit previously assumed the submission price; see TODO #14)."""
        if symbol not in self.active_trades:
            return
        trade = self.active_trades[symbol]
        if trade.get('status') == 'PENDING_EXIT':
            return  # already closing; _check_pending_exit manages fill/repricing

        try:
            # Same BAG contract as entry — a SELL order closes the position
            exit_ibkr_trade = self.broker.place_limit(
                trade['bag_contract'], 'SELL', trade['qty'], current_spread_value
            )
            trade['status'] = 'PENDING_EXIT'
            trade['exit_ibkr_trade'] = exit_ibkr_trade
            trade['exit_reason'] = reason
            trade['exit_limit'] = current_spread_value
            trade['exit_submitted_at'] = market_time.now_et()
            logger.info(
                f"[{symbol}] SUBMITTED CLOSING SPREAD ORDER to IBKR at LIMIT "
                f"${current_spread_value:.2f} (OrderId: {exit_ibkr_trade.order.orderId}). "
                f"Awaiting fill confirmation."
            )
        except Exception as e:
            logger.error(f"[{symbol}] Failed to submit IBKR closing order: {e}")

    def _check_pending_exit(self, symbol: str, trade: dict):
        """Poll a PENDING_EXIT order: book the trade on fill (actual price +
        commissions); reprice if it sits unfilled too long; revert to ACTIVE if
        the order dies so the exit rules can fire again."""
        try:
            self.broker.sleep(0)  # flush event loop so orderStatus is current
            exit_trade = trade['exit_ibkr_trade']
            status = exit_trade.orderStatus.status

            if status == 'Filled':
                fill_price = float(exit_trade.orderStatus.avgFillPrice)
                commission = (self.broker.order_commission(trade.get('ibkr_trade'))
                              if trade.get('ibkr_trade') else 0.0)
                commission += self.broker.order_commission(exit_trade)
                self._finalize_closed_trade(symbol, trade, fill_price, commission)

            elif status in ('Cancelled', 'ApiCancelled', 'Inactive'):
                logger.warning(f"[{symbol}] Closing order {status} — reverting to ACTIVE; exit rules will re-fire.")
                trade['status'] = 'ACTIVE'

            else:
                waited = (market_time.now_et() - trade['exit_submitted_at']).total_seconds()
                if waited >= self._EXIT_REPRICE_AFTER_S:
                    fresh = self.broker.get_spread_value(
                        symbol, trade['direction'], trade['long_strike'], trade['short_strike']
                    )
                    if fresh > 0 and abs(fresh - trade['exit_limit']) >= 0.01:
                        logger.warning(
                            f"[{symbol}] Closing order unfilled for {waited:.0f}s — repricing "
                            f"${trade['exit_limit']:.2f} → ${fresh:.2f}"
                        )
                        self.broker.modify_limit_price(exit_trade, fresh)
                        trade['exit_limit'] = fresh
                        trade['exit_submitted_at'] = market_time.now_et()
                else:
                    logger.info(f"[{symbol}] Closing order still pending (Status: {status}).")
        except Exception as e:
            logger.error(f"[{symbol}] Error checking closing order status: {e}")

    def _finalize_closed_trade(self, symbol: str, trade: dict,
                               fill_price: float, commission: float):
        """Book a confirmed exit: day record, throttle/breaker counters, Discord,
        audit row — all from the ACTUAL fill price and IBKR-reported commissions."""
        reason = trade.get('exit_reason', '')
        profit_pct = (fill_price - trade['entry_price']) / trade['entry_price']
        dollar_pnl = (fill_price - trade['entry_price']) * trade['qty'] * 100
        logger.info(
            f"[{symbol}] CLOSING ORDER FILLED at ${fill_price:.2f} "
            f"(limit was ${trade.get('exit_limit', 0):.2f}) — P&L {profit_pct*100:+.2f}% "
            f"/ ${dollar_pnl:+.2f}, commissions ${commission:.2f}"
        )

        # Record for the post-close day summary
        self.closed_trades_today.append({
            'symbol': symbol, 'direction': trade['direction'],
            'entry_price': trade['entry_price'], 'exit_price': fill_price,
            'profit_pct': profit_pct, 'dollar_pnl': dollar_pnl, 'reason': reason,
            'commission': commission,
        })

        # Track invalidation exits — feeds the entry throttle and the
        # conviction-score penalty for the rest of the day
        if 'Thesis invalidated' in reason:
            key = (symbol, trade['direction'])
            self.invalidation_counts[key] = self.invalidation_counts.get(key, 0) + 1
            if (config.MAX_INVALIDATIONS_PER_SIGNAL > 0 and
                    self.invalidation_counts[key] == config.MAX_INVALIDATIONS_PER_SIGNAL):
                logger.warning(
                    f"[{symbol}] {trade['direction']} signal throttled for the day: "
                    f"{self.invalidation_counts[key]} invalidation exits."
                )
                notifier.notify_throttled(symbol, trade['direction'], self.invalidation_counts[key])

        # Circuit breaker counter
        if profit_pct < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES and not self.circuit_breaker_tripped:
                self.circuit_breaker_tripped = True
                logger.warning(
                    f"CIRCUIT BREAKER TRIPPED: {self.consecutive_losses} consecutive losses. "
                    f"No new entries for the rest of the day."
                )
                notifier.notify_circuit_breaker(self.consecutive_losses)
        else:
            self.consecutive_losses = 0

        # Daily loss limit — the backstop beneath every other guard
        realized = self._realized_pnl_today()
        if (config.MAX_DAILY_LOSS > 0 and not self.daily_loss_limit_hit
                and realized <= -config.MAX_DAILY_LOSS):
            self.daily_loss_limit_hit = True
            logger.warning(
                f"DAILY LOSS LIMIT HIT: realized ${realized:+.2f} (limit -${config.MAX_DAILY_LOSS:.0f}). "
                f"No new entries for the rest of the day."
            )
            notifier.notify_daily_loss_limit(realized, config.MAX_DAILY_LOSS)

        notifier.notify_closed(symbol, trade, fill_price, profit_pct, dollar_pnl,
                               reason, commission=commission)

        # Capture exit-time indicators for the audit log
        df = self.broker.fetch_intraday_data(symbol)
        exit_indicators = trade.get('entry_indicators', {}).copy()
        # >= 30 bars: ADX(14) raises "index out of bounds" below ~29 bars
        if not df.empty and len(df) >= 30:
            strategy.add_indicators(df)
            orb = strategy.orb_levels(df, market_time.now_et())
            if orb is not None:
                exit_indicators = {
                    'adx': df['ADX'].iloc[-1], 'vwap': df['VWAP'].iloc[-1],
                    'orb_high': orb[0], 'orb_low': orb[1],
                    'current_price': df['close'].iloc[-1],
                }

        audit.record(
            "SELL", symbol, trade['direction'], fill_price, reason,
            adx=exit_indicators.get('adx'), vwap=exit_indicators.get('vwap'),
            orb_high=exit_indicators.get('orb_high'), orb_low=exit_indicators.get('orb_low'),
            underlying_price=exit_indicators.get('current_price'),
            profit_pct=profit_pct, dollar_pnl=dollar_pnl,
            peak_pct=trade.get('max_profit_pct'),
            commission=commission,
        )
        self.active_trades.pop(symbol, None)

    def close_all_positions(self, reason: str):
        """Force-close every open position (end-of-day flatten)."""
        for symbol in list(self.active_trades.keys()):
            trade = self.active_trades[symbol]
            try:
                if trade.get('status') == 'PENDING_ENTRY':
                    # Unfilled order lingering near the close — cancel instead of selling
                    self.broker.cancel_order(trade['ibkr_trade'].order)
                    logger.info(f"[{symbol}] EOD: cancelled unfilled entry order.")
                    self.active_trades.pop(symbol, None)
                    continue
                current_spread_value = self.broker.get_spread_value(
                    symbol, trade['direction'], trade['long_strike'], trade['short_strike']
                )
                if current_spread_value <= 0:
                    current_spread_value = 0.01  # still flatten — submit at minimum tick
                self.close_position(symbol, current_spread_value, reason)
            except Exception as e:
                logger.error(f"[{symbol}] EOD flatten failed: {e}")
                self.active_trades.pop(symbol, None)

    # ── End-of-day reporting ─────────────────────────────────────────────────

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

    def _loop_interval(self) -> int:
        """60s normally; FAST_POLL_SECONDS when an exit needs tight watching —
        a closing order in flight, or an ACTIVE trade whose profit is at or
        past FAST_POLL_ARM_PCT (approaching the trail trigger). Fixes the
        sampling slippage where fast moves blew past exit thresholds between
        60-second checks (see TODO #13 / 2026-07-07 retro)."""
        if config.FAST_POLL_SECONDS <= 0:
            return 60
        for trade in self.active_trades.values():
            status = trade.get('status')
            if status == 'PENDING_EXIT':
                return config.FAST_POLL_SECONDS
            if status == 'ACTIVE':
                watermark = max(trade.get('max_profit_pct', 0.0),
                                trade.get('current_profit_pct', 0.0))
                if watermark >= config.FAST_POLL_ARM_PCT:
                    return config.FAST_POLL_SECONDS
        return 60

    def run(self):
        logger.info("Starting 0DTE Options Spread Trading Bot (IBKR)...")
        while True:
            try:
                self.broker.ensure_connected()

                if not market_time.is_market_open():
                    # Market just closed for the day — send the day summary once,
                    # then refresh dashboard.xlsx with the day's trades
                    if self.daily_trade_count > 0 and not self.daily_summary_sent:
                        notifier.notify_day_summary(
                            market_time.now_et().date(),
                            self.closed_trades_today,
                            self.circuit_breaker_tripped,
                        )
                        self.daily_summary_sent = True
                        self.rebuild_dashboard()
                    secs = market_time.seconds_until_market_open()
                    hrs, rem = divmod(secs, 3600)
                    mins = rem // 60
                    if hrs > 0:
                        logger.info(f"Market closed. Next open in {hrs}h {mins}m. Sleeping 1 hour.")
                        self.broker.sleep(3600)   # wake hourly to keep IBKR connection alive
                    else:
                        logger.info(f"Market opens in {mins}m {rem % 60}s. Sleeping until open.")
                        self.broker.sleep(secs)
                    continue

                self.check_and_reset_daily_trade_count()
                self.evaluate_exit_conditions()

                # End-of-day flatten — never hold 0DTE positions into the close
                if market_time.is_eod_flatten_time() and self.active_trades:
                    logger.info("EOD flatten time reached — closing all open positions.")
                    self.close_all_positions("End of day — flattening 0DTE positions")

                # Entry scanning stays on a ~60s cadence even when the loop
                # fast-polls exits — no point re-fetching bars every 15s.
                scan_due = (self._last_entry_scan is None or
                            (market_time.now_et() - self._last_entry_scan).total_seconds() >= 55)
                if scan_due:
                    self._last_entry_scan = market_time.now_et()
                    if not market_time.is_entry_window():
                        logger.info("Entry window closed after 3:00 PM EST; skipping new entries.")
                    else:
                        for symbol in config.SYMBOLS:
                            if symbol not in self.active_trades:
                                direction, reason, indicators = self.evaluate_entry_strategy(symbol)
                                if direction in ("CALL", "PUT"):
                                    self.execute_trade(symbol, direction, reason, indicators)

                interval = self._loop_interval()
                if interval != self._last_interval:
                    logger.info(f"Loop cadence → {interval}s "
                                f"({'fast exit watch' if interval < 60 else 'normal'}).")
                    self._last_interval = interval
                self.broker.sleep(interval)
            except KeyboardInterrupt:
                logger.info("Bot stopped manually.")
                self.broker.disconnect()
                break
            except Exception as e:
                logger.error(f"Unexpected error in main loop: {e}")
                self.broker.sleep(60)
