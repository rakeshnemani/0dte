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
        # Chop tracking. invalidation_counts feeds the entry throttle and only
        # counts LOSING invalidation exits (a signal that exits with profit
        # wasn't proven wrong — 2026-07-08 IWM was throttled after two winners).
        # invalidation_total_today counts ALL invalidation exits and feeds the
        # conviction-score penalty (tape character, not signal quality).
        self.invalidation_counts: Dict[tuple, int] = {}
        self.invalidation_total_today: int = 0
        # (symbol, date) pairs already warned about an untracked account position
        self._untracked_alerted: set = set()
        # Last ET clock-hour an hourly health summary was sent (once per hour)
        self._last_hourly_hour = None
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
            self.invalidation_total_today = 0
            self._untracked_alerted.clear()
            self._last_hourly_hour = None
            self.symbol_lean.clear()
            logger.info(f"Daily trade count reset for {today}")

    def _realized_pnl_today(self) -> float:
        """Realized P&L for the day, net of commissions (confirmed fills only)."""
        return sum(c['dollar_pnl'] - c.get('commission', 0.0)
                   for c in self.closed_trades_today)

    # ── Structure-agnostic helpers (debit spread vs iron condor) ─────────────

    @staticmethod
    def _side(trade: dict) -> int:
        """+1 for long-premium (debit spread), −1 for short-premium (condor).
        profit = side × (value − entry) / entry works for both."""
        return -1 if trade.get('structure') == 'CONDOR' else 1

    def _current_value(self, symbol: str, trade: dict) -> float:
        """Current market value of the trade's package, whatever its shape."""
        if trade.get('structure') == 'CONDOR':
            return self.broker.get_condor_value(
                symbol, trade['short_call'], trade['wing_call'],
                trade['short_put'], trade['wing_put']
            )
        return self.broker.get_spread_value(
            symbol, trade['direction'], trade['long_strike'], trade['short_strike']
        )

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

        # Underlyings with legs in BOTH rights are condor-shaped — adopting each
        # right as a separate "vertical" would misread the position. Alert only.
        multi_right = {u for (u, _r) in groups}
        multi_right = {u for u in multi_right
                       if len({r for (u2, r) in groups if u2 == u}) > 1}

        adopted = []
        for (underlying, right), legs in groups.items():
            longs = [p for p in legs if p.position > 0]
            shorts = [p for p in legs if p.position < 0]
            if (len(longs) != 1 or len(shorts) != 1
                    or underlying in self.active_trades or underlying in multi_right):
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
                'leg_conids': [long_p.contract.conId, short_p.contract.conId],
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

    # Legs must be absent this long before a trade is deemed externally closed
    _RECONCILE_DROP_AFTER_S = 180

    def _trade_leg_conids(self, trade: dict) -> list:
        """All option-leg conIds for a trade (2 for a vertical, 4 for a condor).
        Falls back to the legacy long/short fields for trades opened pre-upgrade."""
        legs = trade.get('leg_conids')
        if legs:
            return legs
        return [c for c in (trade.get('long_conid'), trade.get('short_conid')) if c]

    def _position_still_open(self, trade: dict) -> bool:
        """True if ANY of the trade's legs is still held in the IBKR account.

        Hardened so a feed glitch can NEVER orphan a live position (the 2026-07-09
        bug): fails open on an empty/incomplete positions snapshot, checks every
        leg (not just one), and only the caller's time-based confirmation can
        actually drop a trade.
        """
        leg_conids = self._trade_leg_conids(trade)
        if not leg_conids:
            return True  # nothing to match against — assume open

        # Grace period: let the account feed reflect a just-filled entry first.
        activated_at = trade.get('activated_at')
        if activated_at is not None:
            if (market_time.now_et() - activated_at).total_seconds() < 90:
                return True

        try:
            positions = self.broker.positions()
        except Exception as e:
            logger.warning(f"Position reconciliation fetch failed: {e}")
            return True  # fail-open

        # FAIL OPEN on an empty option-positions feed. An account holding an open
        # 0DTE spread always shows >= 2 option legs; an empty list means the feed
        # isn't populated, NOT that everything closed. (This guard is the specific
        # fix for the orphaning bug.)
        held = {p.contract.conId for p in positions
                if p.contract.secType == 'OPT' and p.position != 0}
        if not held:
            return True

        # Still open if we can see ANY of our legs.
        return any(cid in held for cid in leg_conids)

    def _symbol_option_positions(self, symbol: str) -> list:
        """Account option legs held for `symbol` (root-matched, non-zero)."""
        try:
            positions = self.broker.positions()
        except Exception:
            return []
        root = self.broker.option_symbol(symbol)
        return [p for p in positions if p.contract.secType == 'OPT'
                and p.position != 0 and p.contract.symbol == root]

    def _symbol_has_untracked_position(self, symbol: str) -> bool:
        """True if the account holds option legs for `symbol` that no tracked
        trade owns — a live orphan. Never open on top of one (anti-cascade guard)."""
        held = self._symbol_option_positions(symbol)
        if not held:
            return False
        tracked = set()
        for t in self.active_trades.values():
            tracked.update(self._trade_leg_conids(t))
        return any(p.contract.conId not in tracked for p in held)

    def _alert_untracked_once(self, symbol: str):
        """Fire a ⚠️ alert (once per symbol per day) about an untracked holding
        that's blocking new entries — the human needs to flatten it manually."""
        key = (symbol, market_time.now_et().date())
        if key in self._untracked_alerted:
            return
        self._untracked_alerted.add(key)
        held = self._symbol_option_positions(symbol)
        lines = [f"• {symbol} {p.contract.right}{p.contract.strike:g}  pos {p.position:+g}"
                 for p in held]
        notifier.notify_untracked_holding(symbol, lines)

    # ── Entry ────────────────────────────────────────────────────────────────

    def evaluate_entry_strategy(self, symbol: str):
        """Fetch bars, run the strategy signal, annotate breadth + conviction.
        Returns (direction, reason, indicators)."""
        now = market_time.now_et()
        # #3: indicators come from the signal source (XSP → SPY bars for a real
        # volume-weighted VWAP); strikes/orders still use `symbol` (XSP) below.
        signal_symbol = config.SIGNAL_SOURCE.get(symbol, symbol)
        df = self.broker.fetch_intraday_data(signal_symbol)
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
            # No directional signal — on proven range days, the credit playbook
            # takes over: sell an iron condor around the day's range. Mutually
            # exclusive with debit entries by construction (needs ADX < 22 vs
            # the debit side's ADX >= 25 rising).
            if config.CONDOR_ENABLED:
                ok, c_reason, c_ind = strategy.condor_signal(
                    symbol, df, now,
                    config.STRIKE_STEP.get(symbol, 1),
                    config.SPREAD_WIDTH.get(symbol, 1),
                )
                if ok:
                    return 'CONDOR', c_reason, c_ind
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
            self.symbol_lean, self.invalidation_total_today
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

        # Anti-cascade guard: never open on top of an untracked position in this
        # symbol. If a still-open orphan exists, a new order would pile on (the
        # 2026-07-09 duplicate-IWM-condor cascade). Alert once, don't trade.
        if self._symbol_has_untracked_position(symbol):
            logger.warning(f"[{symbol}] Untracked option position held in account — skipping entry to avoid stacking.")
            self._alert_untracked_once(symbol)
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

        # Minimum conviction to trade at all — below this, the odds and the
        # per-contract fee floor make the trade -EV regardless of size
        conviction = indicators.get('conviction')
        if (config.CONVICTION_SIZING_ENABLED and conviction
                and conviction['score'] < config.MIN_CONVICTION_SCORE):
            logger.info(
                f"[{symbol}] Conviction {conviction['tier']} ({conviction['score']}/5) below "
                f"minimum ({config.MIN_CONVICTION_SCORE}). Skipping entry."
            )
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
                'leg_conids': [long_c.conId, short_c.conId],
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

    def execute_condor(self, symbol: str, reason: str, indicators: dict):
        """Sell an iron condor around the day's range (credit playbook).
        Sized by max loss: (width − credit) × 100 × qty <= MAX_POSITION_SIZE."""
        if symbol in self.active_trades:
            return

        if self._symbol_has_untracked_position(symbol):
            logger.warning(f"[{symbol}] Untracked option position held in account — skipping condor to avoid stacking.")
            self._alert_untracked_once(symbol)
            return

        now_est = market_time.now_et()
        cooldown_expires = self.signal_cooldowns.get((symbol, 'CONDOR'))
        if cooldown_expires and now_est < cooldown_expires:
            return

        if self.circuit_breaker_tripped or self.daily_loss_limit_hit:
            logger.info(f"[{symbol}] Condor signal skipped — risk halt active.")
            return
        if self.daily_trade_count >= config.MAX_TRADES_PER_DAY:
            return

        sc, wc = indicators['short_call'], indicators['wing_call']
        sp, wp = indicators['short_put'], indicators['wing_put']
        width = config.SPREAD_WIDTH.get(symbol, 1)

        credit = self.broker.get_condor_value(symbol, sc, wc, sp, wp)
        if credit <= 0:
            logger.warning(f"[{symbol}] Could not price condor. Aborting.")
            return
        if credit < config.MIN_CONDOR_CREDIT:
            logger.info(f"[{symbol}] Condor credit ${credit:.2f} below minimum "
                        f"${config.MIN_CONDOR_CREDIT:.2f}. Skipping.")
            return
        if credit >= width:   # sanity: max loss would be <= 0 / mispriced quotes
            logger.warning(f"[{symbol}] Condor credit ${credit:.2f} >= width {width} — mispriced. Skipping.")
            return

        max_loss_per = (width - credit) * 100
        qty = int(config.MAX_POSITION_SIZE // max_loss_per)
        if qty < 1:
            logger.info(f"[{symbol}] Condor max loss ${max_loss_per:.0f}/contract exceeds "
                        f"budget ${config.MAX_POSITION_SIZE:.0f}. Skipping.")
            return

        try:
            sc_c = self.broker.get_option_contract(symbol, 'CALL', sc)
            wc_c = self.broker.get_option_contract(symbol, 'CALL', wc)
            sp_c = self.broker.get_option_contract(symbol, 'PUT', sp)
            wp_c = self.broker.get_option_contract(symbol, 'PUT', wp)
            # Positive-value package (see make_bag_multi): SELL opens the condor
            bag = self.broker.make_bag_multi(symbol, [
                (sc_c.conId, 'BUY'), (wc_c.conId, 'SELL'),
                (sp_c.conId, 'BUY'), (wp_c.conId, 'SELL'),
            ])
            ibkr_trade = self.broker.place_limit(bag, 'SELL', qty, credit)

            self.active_trades[symbol] = {
                'structure': 'CONDOR',
                'direction': 'CONDOR',
                'target_entry_price': credit,
                'status': 'PENDING_ENTRY',
                'ibkr_trade': ibkr_trade,
                'bag_contract': bag,
                'qty': qty,
                'max_profit_pct': 0.0,
                'short_call': sc, 'wing_call': wc,
                'short_put': sp, 'wing_put': wp,
                'long_conid': wc_c.conId,
                'short_conid': sc_c.conId,
                # All four legs — reconciliation is "still open if ANY is held"
                'leg_conids': [sc_c.conId, wc_c.conId, sp_c.conId, wp_c.conId],
                'entry_indicators': indicators,
                'reason': reason,
                # Condor "conviction" analog — its own quality metrics, not the
                # debit score (which is inverted for range trades). credit/width
                # is the R:R (higher = better payoff for the max-loss risked).
                'condor_quality': (
                    f"credit/width {credit / width * 100:.0f}% | ADX {indicators.get('adx', 0):.1f} "
                    f"(<{config.CONDOR_MAX_ADX:.0f}) | {indicators.get('vwap_crosses', 0)} VWAP crosses"
                ),
            }
            self.daily_trade_count += 1
            self.signal_cooldowns[(symbol, 'CONDOR')] = (
                now_est + datetime.timedelta(minutes=config.SIGNAL_COOLDOWN_MINUTES)
            )
            logger.info(
                f"SUBMITTED IRON CONDOR to IBKR: {qty}x {symbol} {sp:.0f}/{sc:.0f} "
                f"(wings ±{width}) at CREDIT ${credit:.2f} — max loss "
                f"${max_loss_per * qty:.0f} (OrderId: {ibkr_trade.order.orderId})"
            )
            notifier.notify_condor_submit(symbol, sc, wc, sp, wp, credit, qty,
                                          max_loss_per * qty, reason,
                                          ibkr_trade.order.orderId,
                                          self.active_trades[symbol]['condor_quality'])
            notifier.notify_today_summary(self.active_trades, self.closed_trades_today)
        except Exception as e:
            logger.error(f"Failed to place IBKR condor order for {symbol}: {e}")

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

        # Resting take-profit filled? Must be checked BEFORE reconciliation —
        # a filled TP removes the legs from the account, which would otherwise
        # be misread as an external close (and its P&L discarded).
        if trade.get('status') == 'ACTIVE' and self._tp_filled(symbol, trade):
            return

        # Reconcile against the real IBKR account. Only conclude "externally
        # closed" after the legs have been consistently absent for a sustained
        # window — TIME-based, not loop-count, so 15s fast-polling can't drop a
        # live trade in 30s (the 2026-07-09 orphaning bug). Combined with the
        # fail-open guards in _position_still_open, a feed glitch cannot orphan.
        if trade.get('status') == 'ACTIVE':
            if self._position_still_open(trade):
                trade.pop('first_missing_at', None)
            else:
                first = trade.get('first_missing_at')
                if first is None:
                    trade['first_missing_at'] = market_time.now_et()
                    logger.info(f"[{symbol}] Legs not found in account — starting {self._RECONCILE_DROP_AFTER_S}s confirmation window.")
                    return
                missing_for = (market_time.now_et() - first).total_seconds()
                if missing_for >= self._RECONCILE_DROP_AFTER_S:
                    logger.warning(f"[{symbol}] Legs absent for {missing_for:.0f}s — confirmed closed externally. Dropping from tracking.")
                    self._cancel_tp_order(symbol, trade)  # never leave a resting order behind
                    notifier.notify_closed_externally(symbol, trade['direction'])
                    self.active_trades.pop(symbol, None)
                else:
                    logger.info(f"[{symbol}] Legs still absent ({missing_for:.0f}/{self._RECONCILE_DROP_AFTER_S}s); re-checking.")
                return  # skip exit eval while the position's status is uncertain

        current_spread_value = self._current_value(symbol, trade)
        if current_spread_value <= 0:
            logger.warning(f"Could not fetch spread value for {symbol}. Skipping exit eval.")
            return

        if trade.get('status') == 'PENDING_ENTRY':
            self._check_pending_fill(symbol, trade)
            return

        # side: +1 long premium (debit), −1 short premium (condor)
        profit_pct = self._side(trade) * (current_spread_value - trade['entry_price']) / trade['entry_price']

        # Cache live P&L so the "today" summary can read it without extra IBKR calls
        trade['current_value'] = current_spread_value
        trade['current_profit_pct'] = profit_pct

        if profit_pct > trade['max_profit_pct']:
            trade['max_profit_pct'] = profit_pct
            if profit_pct > 0:
                logger.info(f"[{symbol}] New Max Profit: {profit_pct*100:.2f}%")

        # Thesis-invalidation check — debit: sustained VWAP recross; condor:
        # price closed beyond a short strike (range thesis dead). 1-min bars
        # change at most once a minute, so cache the verdict ~50s — fast
        # polling shouldn't multiply bar fetches.
        is_condor = trade.get('structure') == 'CONDOR'
        invalidated = trade.get('_inval_last', False)
        if config.VWAP_INVALIDATION_BARS > 0 or is_condor:
            checked_at = trade.get('_inval_checked_at')
            if checked_at is None or (market_time.now_et() - checked_at).total_seconds() >= 50:
                try:
                    # #3: invalidation reads the signal source too (XSP → SPY),
                    # so the VWAP recross is measured on real volume, not thin XSP.
                    df = self.broker.fetch_intraday_data(config.SIGNAL_SOURCE.get(symbol, symbol))
                    if is_condor:
                        invalidated = strategy.condor_breached(
                            df, trade['short_call'], trade['short_put'])
                    else:
                        invalidated = strategy.thesis_invalidated(trade['direction'], df)
                except Exception as e:
                    logger.warning(f"[{symbol}] Invalidation check failed: {e}")
                trade['_inval_checked_at'] = market_time.now_et()
                trade['_inval_last'] = invalidated

        inval_reason = None
        if is_condor:
            inval_reason = (
                f"Range breached: price closed beyond a short strike "
                f"({trade['short_put']:.0f}/{trade['short_call']:.0f}) for "
                f"{strategy.CONDOR_BREACH_BARS} consecutive bars"
            )
        exit_triggered, exit_reason = strategy.exit_decision(
            profit_pct, trade['max_profit_pct'], invalidated,
            invalidation_reason=inval_reason
        )
        if exit_triggered:
            logger.info(f"[{symbol}] EXIT TRIGGERED: {exit_reason}")
            self.close_position(symbol, current_spread_value, exit_reason)

    def _tp_filled(self, symbol: str, trade: dict) -> bool:
        """If the resting take-profit limit filled, book the trade from its
        actual fill. Returns True when the trade was finalized (or the TP order
        died and was cleared — caller should just continue next loop)."""
        tp = trade.get('tp_ibkr_trade')
        if tp is None:
            return False
        try:
            self.broker.sleep(0)
            status = tp.orderStatus.status
            if status == 'Filled':
                fill_price = float(tp.orderStatus.avgFillPrice)
                commission = (self.broker.order_commission(trade.get('ibkr_trade'))
                              if trade.get('ibkr_trade') else 0.0)
                commission += self.broker.order_commission(tp)
                tp_pct = (config.CONDOR_TP_PCT if trade.get('structure') == 'CONDOR'
                          else config.TAKE_PROFIT_TARGET_PCT)
                trade['exit_reason'] = (
                    f"Take-profit target +{tp_pct*100:.0f}% hit "
                    f"(resting limit ${trade.get('tp_price', 0):.2f})"
                )
                trade['exit_permId'] = self.broker.order_perm_id(tp)
                logger.info(f"[{symbol}] TAKE-PROFIT FILLED at ${fill_price:.2f}")
                self._finalize_closed_trade(symbol, trade, fill_price, commission)
                return True
            if status in ('Cancelled', 'ApiCancelled', 'Inactive'):
                logger.warning(f"[{symbol}] Resting take-profit order {status} — clearing; loop exits still protect.")
                trade.pop('tp_ibkr_trade', None)
        except Exception as e:
            logger.error(f"[{symbol}] Error checking take-profit order: {e}")
        return False

    def _cancel_tp_order(self, symbol: str, trade: dict) -> bool:
        """Cancel a resting take-profit before another exit path submits its own
        sell — otherwise a late TP fill would open a naked short spread.
        Returns True if the TP turned out to have FILLED (won the race) and the
        trade was finalized — callers must then abort their own exit."""
        tp = trade.pop('tp_ibkr_trade', None)
        if tp is None:
            return False
        try:
            if not tp.isDone():
                self.broker.cancel_order(tp.order)
                self.broker.sleep(1)  # let the cancel/fill race resolve
            if tp.orderStatus.status == 'Filled':
                fill_price = float(tp.orderStatus.avgFillPrice)
                commission = (self.broker.order_commission(trade.get('ibkr_trade'))
                              if trade.get('ibkr_trade') else 0.0)
                commission += self.broker.order_commission(tp)
                tp_pct = (config.CONDOR_TP_PCT if trade.get('structure') == 'CONDOR'
                          else config.TAKE_PROFIT_TARGET_PCT)
                trade['exit_reason'] = (
                    f"Take-profit target +{tp_pct*100:.0f}% hit "
                    f"(filled during cancel race)"
                )
                trade['exit_permId'] = self.broker.order_perm_id(tp)
                logger.info(f"[{symbol}] Take-profit filled just before cancellation — booking it.")
                self._finalize_closed_trade(symbol, trade, fill_price, commission)
                return True
        except Exception as e:
            logger.warning(f"[{symbol}] Error cancelling take-profit order: {e}")
        return False

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
                # permId — the permanent, account-wide key for this trade
                trade['entry_permId'] = self.broker.order_perm_id(ibkr_trade)

                # Park a resting take-profit limit. Debit: SELL at entry×(1+target).
                # Condor: BUY back at credit×(1−CONDOR_TP_PCT). Fills between
                # heartbeats (no sampling loss) and trades with the move, not
                # against it. Every other exit path cancels it first.
                if trade.get('structure') == 'CONDOR':
                    tp_price = (round(filled_price * (1 - config.CONDOR_TP_PCT), 2)
                                if config.CONDOR_TP_PCT > 0 else None)
                    tp_action = 'BUY'
                else:
                    tp_price = (round(filled_price * (1 + config.TAKE_PROFIT_TARGET_PCT), 2)
                                if config.TAKE_PROFIT_TARGET_PCT > 0 else None)
                    tp_action = 'SELL'
                if tp_price and tp_price > 0:
                    try:
                        trade['tp_ibkr_trade'] = self.broker.place_limit(
                            trade['bag_contract'], tp_action, trade['qty'], tp_price
                        )
                        trade['tp_price'] = tp_price
                        logger.info(f"[{symbol}] Take-profit limit ({tp_action}) resting at ${tp_price:.2f}.")
                    except Exception as e:
                        logger.warning(f"[{symbol}] Could not place take-profit order: {e}")

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
                    perm_id=trade.get('entry_permId'),
                )
                if trade.get('structure') == 'CONDOR':
                    notifier.notify_condor_filled(symbol, trade, filled_price)
                else:
                    notifier.notify_filled(symbol, trade, filled_price)

            elif status in ('Cancelled', 'ApiCancelled', 'Inactive'):
                logger.warning(f"[{symbol}] IBKR order {status}. Removing from tracking.")
                self.active_trades.pop(symbol, None)
            else:
                logger.info(f"[{symbol}] IBKR order still pending (Status: {status}).")
        except Exception as e:
            logger.error(f"[{symbol}] Error checking IBKR order status: {e}")

    def _book_exit_fill(self, symbol: str, trade: dict, exit_trade):
        """Book a close from a confirmed fill: price + commissions + permId,
        then finalize. Shared by the Filled path and the double-fill guard."""
        fill_price = float(exit_trade.orderStatus.avgFillPrice)
        commission = (self.broker.order_commission(trade.get('ibkr_trade'))
                      if trade.get('ibkr_trade') else 0.0)
        commission += self.broker.order_commission(exit_trade)
        trade['exit_permId'] = self.broker.order_perm_id(exit_trade)
        self._finalize_closed_trade(symbol, trade, fill_price, commission)

    def _book_partial_exit(self, symbol: str, trade: dict, exit_trade, filled_qty: int):
        """A closing order died after PARTIALLY filling: book the filled slice
        (audit + day record) and shrink the tracked qty so the retry closes only
        the true remainder. Counters (throttle/breaker) are left to the final close."""
        fill_price = float(exit_trade.orderStatus.avgFillPrice)
        side = self._side(trade)
        profit_pct = side * (fill_price - trade['entry_price']) / trade['entry_price']
        dollar_pnl = side * (fill_price - trade['entry_price']) * filled_qty * 100
        commission = self.broker.order_commission(exit_trade)
        reason = f"{trade.get('exit_reason', '')} [PARTIAL {filled_qty}/{trade['qty']} before order died]"
        logger.warning(f"[{symbol}] Closing order died after partial fill "
                       f"{filled_qty}/{trade['qty']} at ${fill_price:.2f} — booking the slice.")
        self.closed_trades_today.append({
            'symbol': symbol, 'direction': trade['direction'],
            'entry_price': trade['entry_price'], 'exit_price': fill_price,
            'profit_pct': profit_pct, 'dollar_pnl': dollar_pnl, 'reason': reason,
            'commission': commission,
        })
        audit.record(
            "SELL", symbol, trade['direction'], fill_price, reason,
            profit_pct=profit_pct, dollar_pnl=dollar_pnl,
            peak_pct=trade.get('max_profit_pct'), commission=commission,
            perm_id=self.broker.order_perm_id(exit_trade),
        )
        notifier.notify_closed(symbol, trade, fill_price, profit_pct, dollar_pnl,
                               reason, commission=commission)
        trade['qty'] -= filled_qty

    def _remaining_qty(self, trade: dict):
        """How much of this position the account ACTUALLY still holds, measured
        on the always-long reference leg (debit long leg / condor wing call).
        None = unknown (feed unavailable) → caller must defer, not act.
        0 = gone. Negative = OVER-CLOSED (inverse position — critical)."""
        pos = self.broker.position_qty(trade.get('long_conid'))
        return None if pos is None else int(pos)

    # Reprice an unfilled closing order after this many seconds
    _EXIT_REPRICE_AFTER_S = 180
    # Break the reject/retry loop (TODO #26): space out close attempts, and
    # after this many rejections stop auto-retrying and alert a human.
    _MAX_CLOSE_ATTEMPTS = 4
    _CLOSE_RETRY_COOLDOWN_S = 30

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
        if trade.get('close_failed'):
            return  # gave up after repeated rejections — awaiting manual close / expiry

        # Space out retries so a rejected order can't be re-submitted every loop
        # (the 2026-07-09 error-201 loop fired ~every 15s until the close).
        last = trade.get('last_close_attempt_at')
        if last and (market_time.now_et() - last).total_seconds() < self._CLOSE_RETRY_COOLDOWN_S:
            return

        # Give up after N attempts rather than hammer forever — keep the trade
        # tracked (never orphan it), alert, and let the human or expiry resolve it.
        attempts = trade.get('close_attempts', 0)
        if attempts >= self._MAX_CLOSE_ATTEMPTS:
            trade['close_failed'] = True
            code, msg = trade.get('last_close_error', (0, ''))
            logger.error(
                f"[{symbol}] Close FAILED after {attempts} attempts "
                f"(last error {code}: {msg[:120]}). Halting auto-retry — manual intervention needed."
            )
            notifier.notify_close_failed(symbol, trade.get('direction', ''), attempts, code, msg)
            return

        # Cancel any resting take-profit first — if it filled during the cancel
        # race, the trade is already booked and this exit must abort.
        if self._cancel_tp_order(symbol, trade):
            return

        # #30 double-fill guard: never trust order status — requantify against
        # the ACTUAL account position before submitting any close.
        remaining = self._remaining_qty(trade)
        if remaining is None:
            logger.warning(f"[{symbol}] Position feed unavailable — deferring close attempt to next loop.")
            return
        if remaining < 0:
            # A prior close executed MORE than the position — inverse position!
            logger.error(f"[{symbol}] OVER-CLOSED: reference leg position {remaining} "
                         f"(inverse). Halting all automatic action — manual flatten required.")
            notifier.notify_over_closed(symbol, trade.get('direction', ''), remaining)
            trade['close_failed'] = True   # halt; keep tracked for visibility
            return
        if remaining == 0:
            # Position is gone. If OUR previous closing order actually filled
            # (e.g. reported Cancelled but executed — the 2026-07-10 bug), book it.
            prior = trade.get('exit_ibkr_trade')
            if prior is not None and float(prior.orderStatus.filled or 0) > 0:
                logger.warning(f"[{symbol}] Prior 'dead' closing order actually filled — booking it instead of resubmitting.")
                self._book_exit_fill(symbol, trade, prior)
            else:
                logger.warning(f"[{symbol}] Nothing left to close in the account — treating as externally closed.")
                notifier.notify_closed_externally(symbol, trade.get('direction', ''))
                self.active_trades.pop(symbol, None)
            return
        if remaining < trade['qty']:
            logger.warning(f"[{symbol}] Account holds {remaining}/{trade['qty']} — a prior close "
                           f"partially executed. Closing only the remaining {remaining}.")
            trade['qty'] = remaining

        try:
            # Retry attempts: sweep stray open orders on these legs first so the
            # resubmit can't be rejected with error 201 (opposite-side order)
            if attempts >= 1:
                swept = self.broker.cancel_open_orders_for(self.broker.option_symbol(symbol))
                if swept:
                    logger.warning(f"[{symbol}] Swept {swept} stray open order(s) before close retry.")
                    self.broker.sleep(1)

            # Same BAG contract as entry; closing action is the opposite of
            # opening — SELL closes a debit spread, BUY closes a condor
            close_action = 'BUY' if trade.get('structure') == 'CONDOR' else 'SELL'
            exit_ibkr_trade = self.broker.place_limit(
                trade['bag_contract'], close_action, trade['qty'], current_spread_value
            )
            trade['status'] = 'PENDING_EXIT'
            trade['exit_ibkr_trade'] = exit_ibkr_trade
            trade['exit_reason'] = reason
            trade['exit_limit'] = current_spread_value
            trade['exit_submitted_at'] = market_time.now_et()
            trade['close_attempts'] = attempts + 1
            trade['last_close_attempt_at'] = market_time.now_et()
            logger.info(
                f"[{symbol}] SUBMITTED CLOSING SPREAD ORDER to IBKR at LIMIT "
                f"${current_spread_value:.2f} (OrderId: {exit_ibkr_trade.order.orderId}, "
                f"attempt {attempts + 1}/{self._MAX_CLOSE_ATTEMPTS}). Awaiting fill confirmation."
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
                self._book_exit_fill(symbol, trade, exit_trade)

            elif status in ('Cancelled', 'ApiCancelled', 'Inactive'):
                # #30: a "dead" order may have EXECUTED anyway — check its fills
                # before believing the status (2026-07-10: a Cancelled close had
                # filled; the blind resubmit double-closed into an inverse position).
                filled_qty = float(exit_trade.orderStatus.filled or 0)
                if filled_qty >= trade['qty'] - 1e-9:
                    logger.warning(f"[{symbol}] Closing order reported {status} but FULLY FILLED "
                                   f"({filled_qty:g}/{trade['qty']}) — booking the fill.")
                    self._book_exit_fill(symbol, trade, exit_trade)
                    return
                if filled_qty > 0:
                    self._book_partial_exit(symbol, trade, exit_trade, int(filled_qty))
                    # fall through: remainder reverts to ACTIVE for a (requantified) retry

                code, msg = self.broker.last_order_error(exit_trade)
                trade['last_close_error'] = (code, msg)
                # Error 201 = an order already exists on the opposite side of a
                # leg. Sweep our stray open orders on this underlying so the
                # retry isn't rejected for the same reason.
                if code == 201:
                    root = self.broker.option_symbol(symbol)
                    cleared = self.broker.cancel_open_orders_for(
                        root, except_order_id=exit_trade.order.orderId)
                    if cleared:
                        logger.warning(f"[{symbol}] Cleared {cleared} conflicting open order(s) after error 201.")
                logger.warning(
                    f"[{symbol}] Closing order {status} (error {code}: {msg[:100]}) — "
                    f"reverting to ACTIVE; retry gated by {self._CLOSE_RETRY_COOLDOWN_S}s cooldown "
                    f"(attempt {trade.get('close_attempts', 0)}/{self._MAX_CLOSE_ATTEMPTS}); "
                    f"retry will requantify against the account first."
                )
                trade['status'] = 'ACTIVE'

            else:
                waited = (market_time.now_et() - trade['exit_submitted_at']).total_seconds()
                if waited >= self._EXIT_REPRICE_AFTER_S:
                    fresh = self._current_value(symbol, trade)
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
        side = self._side(trade)   # +1 debit, −1 condor (short premium)
        profit_pct = side * (fill_price - trade['entry_price']) / trade['entry_price']
        dollar_pnl = side * (fill_price - trade['entry_price']) * trade['qty'] * 100
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

        # Track invalidation exits. ALL of them feed the conviction penalty
        # (tape character); only LOSING ones (< -10%) count toward the throttle —
        # a signal that exits with profit wasn't proven wrong, just early.
        if 'Thesis invalidated' in reason:
            self.invalidation_total_today += 1
            if profit_pct < -0.10:
                key = (symbol, trade['direction'])
                self.invalidation_counts[key] = self.invalidation_counts.get(key, 0) + 1
                if (config.MAX_INVALIDATIONS_PER_SIGNAL > 0 and
                        self.invalidation_counts[key] == config.MAX_INVALIDATIONS_PER_SIGNAL):
                    logger.warning(
                        f"[{symbol}] {trade['direction']} signal throttled for the day: "
                        f"{self.invalidation_counts[key]} losing invalidation exits."
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

        # Capture exit-time indicators for the audit log — from the signal source
        # (#3: XSP → SPY), so exit indicators are comparable to the entry ones and
        # aren't computed from thin XSP index bars.
        df = self.broker.fetch_intraday_data(config.SIGNAL_SOURCE.get(symbol, symbol))
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
            perm_id=trade.get('exit_permId'),
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
                current_spread_value = self._current_value(symbol, trade)
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

                # Hourly health heartbeat (once per ET clock hour) — surfaces
                # awaiting-fill / open / closed within the hour, not just at EOD.
                cur_hour = market_time.now_et().hour
                if cur_hour != self._last_hourly_hour:
                    self._last_hourly_hour = cur_hour
                    notifier.notify_hourly_health(self.active_trades, self.closed_trades_today)

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
                                elif direction == 'CONDOR':
                                    self.execute_condor(symbol, reason, indicators)

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
