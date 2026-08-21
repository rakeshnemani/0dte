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
import commands
import config
import gex
import market_time
import notifier
import strategy
from broker import IBKRBroker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class TradingBot:
    def __init__(self):
        self.broker = IBKRBroker()
        self.broker.on_farm_change = notifier.notify_data_farm   # data-feed drop/restore → Discord
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
        # (symbol, date) pairs already warned about an untracked account position
        self._untracked_alerted: set = set()
        # Last ET clock-hour an hourly health summary was sent (once per hour)
        self._last_hourly_hour = None
        # Entry scans stay on a ~60s cadence even when the loop fast-polls exits
        self._last_entry_scan = None
        self._last_interval: int = 60
        # GEX chain cache (STRATEGY=='gex'): OI is once-daily, so fetch the chain
        # occasionally and recompute Gflip intraday from it as spot moves.
        self._gex_chain: list = []
        self._gex_chain_at = None
        self._gex_collect_at = None   # throttles the always-on GEX data collection (~60s)
        # Throttle "signal formed but skipped" transparency alerts (per strategy:symbol).
        self._blocked_alert_at: Dict[tuple, datetime.datetime] = {}

        # Thesis-GEX command rail (#44): a human-authorised thesis dropped as a JSON command
        # in this dir → the bot watches its trigger and fires a single-leg 'thesis:SPX' trade.
        # Pending arms/close-ifs are rebuilt from the files each loop (files are the source of
        # truth, so a restart resumes them); one-shot close/cancel are executed then moved.
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._thesis_dir = (config.THESIS_COMMAND_DIR
                            if os.path.isabs(config.THESIS_COMMAND_DIR)
                            else os.path.join(repo_root, config.THESIS_COMMAND_DIR))
        self._thesis_arms: list = []       # pending armed orders  (rebuilt from files)
        self._thesis_closers: list = []    # pending conditional closes (rebuilt from files)
        self._thesis_seen: set = set()     # command ids already announced (notify each arm once)

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
            self._untracked_alerted.clear()
            self._last_hourly_hour = None
            logger.info(f"Daily trade count reset for {today}")

    def _realized_pnl_today(self) -> float:
        """Realized P&L for the day, net of commissions (confirmed fills only)."""
        return sum(c['dollar_pnl'] - c.get('commission', 0.0)
                   for c in self.closed_trades_today)

    # ── Trade helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _side(trade: dict) -> int:
        """+1 — a single long option is long premium; profit = (value−entry)/entry."""
        return 1

    def _current_value(self, symbol: str, trade: dict) -> float:
        """Current market value of the single long option (its mid)."""
        return self.broker._get_option_mid(trade['option_contract'])

    # ── Startup adoption / reconciliation ────────────────────────────────────

    def adopt_orphan_positions(self):
        """A restart wipes active_trades. We run single-leg only and can't know which
        strategy (trend/gex) a lone leg belonged to — so we don't auto-adopt (a mis-routed
        exit is worse than none). Instead: alert on any untracked option position the
        account still holds, so it's handled manually or by the daily 3:55 flatten.
        Guidance: don't restart with a position open."""
        try:
            positions = self.broker.positions()
        except Exception as e:
            logger.warning(f"Startup position scan failed: {e}")
            return
        opts = [p for p in positions if p.contract.secType == 'OPT' and p.position != 0]
        if not opts:
            return
        lines = [f"• {p.contract.localSymbol or p.contract.symbol}  pos {p.position:+.0f}"
                 for p in opts]
        logger.warning(f"Untracked option positions at startup: {len(opts)} — NOT adopted "
                       f"(single-leg strategy unknown; manage manually / via EOD flatten).")
        notifier.notify_unadoptable(lines)

    # Legs must be absent this long before a trade is deemed externally closed
    _RECONCILE_DROP_AFTER_S = 180

    def _trade_leg_conids(self, trade: dict) -> list:
        """Option-leg conId(s) for a trade (one for a single long leg)."""
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
        # 0DTE position shows its option leg; an empty list means the feed
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

    def _scan_trend_entries(self):
        """Trend-mode entry scan (STRATEGY == 'trend'): only inside a TREND_WINDOWS
        slot, one position per symbol — the Supertrend flip + PSAR + Kaufman
        signal, single ATM leg, fixed 1 contract."""
        if not market_time.in_trend_window():
            return
        for symbol in config.SYMBOLS:
            if self._tkey('trend', symbol) in self.active_trades:
                continue
            direction, reason, indicators = self.evaluate_trend_entry(symbol)
            if direction in ('CALL', 'PUT'):
                self.execute_trade(symbol, direction, reason, indicators, 'trend')

    def evaluate_trend_entry(self, symbol: str):
        """Run the trend signal on the symbol's OWN 1-min bars. Supertrend/PSAR/
        Kaufman need only high/low/close, so SPX index bars are used directly (not
        the SPY signal-source proxy the breakout VWAP needs). Returns (direction,
        reason, indicators)."""
        now = market_time.now_et()
        df = self.broker.fetch_intraday_data(symbol)
        if df.empty or len(df) < config.TREND_MIN_BARS:
            return None, "", {}
        direction, reason, indicators, _ = strategy.trend_entry_signal(symbol, df, now)
        if direction and config.TREND_SKIP_LOWIV > 0:
            ive = strategy.entry_realized_vol(df)      # open→now, no lookahead
            if ive < config.TREND_SKIP_LOWIV:
                logger.info(f"[{symbol}] Low-vol skip: entry-time vol {ive:.3f} < "
                            f"{config.TREND_SKIP_LOWIV} — quiet day, a naked long would just bleed theta.")
                self._alert_blocked('trend', symbol,
                                    f"{direction} flip formed but SKIPPED: entry-vol {ive:.3f} < "
                                    f"{config.TREND_SKIP_LOWIV} — slow tape, theta would eat a naked long")
                return None, "", {}
            indicators['iv_entry'] = round(ive, 3)
        if direction:
            logger.info(f"[{symbol}] TREND SIGNAL: {reason} (entry-vol {indicators.get('iv_entry', 'n/a')})")
        elif reason:                          # a flip formed but the kauf gate blocked it
            self._alert_blocked('trend', symbol, reason)
        return direction, reason, indicators

    # ── GEX strategy (STRATEGY=='gex') ───────────────────────────────────────
    def _refresh_gex_chain(self, symbol: str):
        """Re-fetch the OI+IV chain if the cache is empty or older than GEX_REFRESH_MIN
        (OI is a once-daily number, so intraday refreshes are cheap-ish and rare)."""
        now = market_time.now_et()
        fresh = (self._gex_chain_at is not None and
                 (now - self._gex_chain_at).total_seconds() < config.GEX_REFRESH_MIN * 60)
        if self._gex_chain and fresh:
            return
        spot, chain = self.broker.fetch_gex_chain(
            symbol, config.GEX_CHAIN_STRIKE_PCT, config.GEX_CHAIN_EXPIRIES, config.GEX_CHAIN_MAX_STRIKES)
        if chain:
            self._gex_chain = chain
            self._gex_chain_at = now
            logger.info(f"[{symbol}] GEX chain refreshed: {len(chain)} strike-expiries.")
            self._save_gex_chain(symbol, spot, chain)
        elif not self._gex_chain:
            logger.warning(f"[{symbol}] GEX chain fetch returned empty — no regime read this scan.")

    def _save_gex_chain(self, symbol: str, spot: float, chain: list):
        """Persist the live GEX chain (OI + IV per strike) on every refresh. Historical
        GEX data is expensive to buy, so we accumulate our OWN dataset here — one CSV per
        day (data/gex/chain_YYYY-MM-DD.csv), which later lets us backtest GEX for real."""
        try:
            import csv
            d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'gex')
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"chain_{market_time.now_et():%Y-%m-%d}.csv")
            new = not os.path.isfile(path)
            gf = gex.gamma_flip(chain, spot)
            ts = market_time.now_et().strftime('%Y-%m-%d %H:%M:%S')
            with open(path, 'a', newline='') as f:
                w = csv.writer(f)
                if new:
                    w.writerow(['timestamp', 'symbol', 'spot', 'gflip', 'strike',
                                'oi_call', 'oi_put', 'iv', 'T'])
                for c in chain:
                    w.writerow([ts, symbol, round(spot, 2), round(gf, 2) if gf else '',
                                c['strike'], int(c['oi_call']), int(c['oi_put']),
                                round(c['iv'], 4), round(c['T'], 6)])
        except Exception as e:
            logger.warning(f"[{symbol}] Could not save GEX chain snapshot: {e}")

    def _collect_gex_data(self, symbol: str):
        """ALWAYS-ON GEX data collection — runs every loop regardless of position state or
        entry window, so the chain, Gflip and distance-to-flip are recorded even while a GEX
        trade is open (on 2026-08-17 the bot went data-blind after entry because collection
        was tied to the entry scan). Refreshes the chain (30-min throttle), and every ~5 min
        logs the regime + distance-to-flip and appends it to data/gex/regime_YYYY-MM-DD.csv.
        Data collection ONLY — it makes no trading decision and changes no entry/exit rule."""
        now = market_time.now_et()
        if self._gex_collect_at and (now - self._gex_collect_at).total_seconds() < 60:
            return
        self._gex_collect_at = now
        self._refresh_gex_chain(symbol)              # 30-min throttle inside; also saves the chain
        if not self._gex_chain:
            return
        last = getattr(self, '_gex_log_at', None)
        if last is not None and (now - last).total_seconds() < 300:
            return                                    # log/record the regime ~every 5 min
        spot = self.broker.get_current_price(symbol)
        if spot <= 0:
            return
        self._gex_log_at = now
        gflip = gex.gamma_flip(self._gex_chain, spot)
        zones = gex.concentration_zones(self._gex_chain, n=3)
        reg = gex.gex_regime(spot, gflip)
        gfs = f"{gflip:.1f}" if gflip else "n/a"
        cw = zones.get('call_walls', [[0]])[0][0]; pw = zones.get('put_walls', [[0]])[0][0]
        dist_pts = (spot - gflip) if gflip else None
        dist_pct = (dist_pts / gflip * 100) if gflip else None
        dstr = f"{dist_pts:+.1f}pt ({dist_pct:+.3f}%)" if dist_pts is not None else "n/a"
        in_pos = self._tkey('gex', symbol) in self.active_trades
        logger.info(f"[{symbol}] GEX regime: spot {spot:.1f} vs Gflip {gfs} → {reg} gamma"
                    f" | dist {dstr} | walls C{cw:.0f}/P{pw:.0f}"
                    f"{' | IN-POSITION' if in_pos else ''}")
        self._save_gex_regime(symbol, spot, gflip, dist_pts, dist_pct, reg, cw, pw, in_pos)

    def _save_gex_regime(self, symbol, spot, gflip, dist_pts, dist_pct, regime, cw, pw, in_pos):
        """Append one distance-to-flip row to data/gex/regime_YYYY-MM-DD.csv — the evidence
        dataset for 'does entering near Gflip chop?'. Written every ~5 min. Data only."""
        try:
            import csv
            d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'gex')
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"regime_{market_time.now_et():%Y-%m-%d}.csv")
            new = not os.path.isfile(path)
            with open(path, 'a', newline='') as f:
                w = csv.writer(f)
                if new:
                    w.writerow(['timestamp', 'symbol', 'spot', 'gflip', 'dist_pts',
                                'dist_pct', 'regime', 'call_wall', 'put_wall', 'in_position'])
                w.writerow([market_time.now_et().strftime('%Y-%m-%d %H:%M:%S'), symbol,
                            round(spot, 2), round(gflip, 2) if gflip else '',
                            round(dist_pts, 2) if dist_pts is not None else '',
                            round(dist_pct, 4) if dist_pct is not None else '',
                            regime, f"{cw:.0f}", f"{pw:.0f}", int(in_pos)])
        except Exception as e:
            logger.warning(f"[{symbol}] Could not save GEX regime row: {e}")

    @staticmethod
    def _tkey(strategy: str, symbol: str) -> str:
        """Composite active_trades key so multiple strategies can each hold a position in
        the same symbol at once (e.g. 'trend:SPX' and 'gex:SPX')."""
        return f"{strategy}:{symbol}"

    def _alert_blocked(self, strategy: str, symbol: str, reason: str):
        """Discord alert when a setup FORMED but no trade was placed (a filter blocked it) —
        for process transparency. Throttled to once per 10 min per (strategy, symbol)."""
        now = market_time.now_et()
        key = (strategy, symbol)
        last = self._blocked_alert_at.get(key)
        if last and (now - last).total_seconds() < 600:
            return
        self._blocked_alert_at[key] = now
        logger.info(f"[{symbol}] {strategy} setup formed but skipped → {reason}")
        notifier.notify_signal_blocked(strategy, symbol, reason)

    def _scan_gex_entries(self):
        """GEX entry scan: only inside a GEX_WINDOWS slot. Refresh the chain, recompute
        Gflip + walls at the current spot, run the 3-condition signal."""
        if not market_time.in_gex_window():
            return
        for symbol in config.SYMBOLS:
            if self._tkey('gex', symbol) in self.active_trades:
                continue
            self._refresh_gex_chain(symbol)
            direction, reason, indicators = self.evaluate_gex_entry(symbol)
            if direction in ('CALL', 'PUT'):
                self.execute_trade(symbol, direction, reason, indicators, 'gex')

    def evaluate_gex_entry(self, symbol: str):
        """Recompute Gflip + concentration zones from the cached chain at the current spot,
        then apply the 3-condition GEX entry (regime + OR breakout + momentum)."""
        now = market_time.now_et()
        df = self.broker.fetch_intraday_data(symbol)
        if df.empty or len(df) < 18 or not self._gex_chain:
            return None, "", {}
        spot = float(df['close'].iloc[-1])
        gflip = gex.gamma_flip(self._gex_chain, spot)
        zones = gex.concentration_zones(self._gex_chain, n=3)
        # (regime + distance-to-flip logging/recording is done every loop in
        # _collect_gex_data — always-on, so it keeps running while a position is held.)
        direction, reason, indicators, _ = strategy.gex_entry_signal(
            symbol, df, now, gflip, zones)
        if direction and config.GEX_SKIP_LOWIV > 0:
            ive = strategy.entry_realized_vol(df)      # open→now, no lookahead
            if ive < config.GEX_SKIP_LOWIV:
                logger.info(f"[{symbol}] GEX low-vol skip: entry-time vol {ive:.3f} < "
                            f"{config.GEX_SKIP_LOWIV} — slow tape, theta would eat the naked leg.")
                self._alert_blocked('gex', symbol,
                                    f"{direction}: full GEX setup formed but SKIPPED — entry-vol {ive:.3f} < "
                                    f"{config.GEX_SKIP_LOWIV} (slow tape, theta would eat the naked leg)")
                return None, "", {}
            indicators['iv_entry'] = round(ive, 3)
        if direction:
            # Freeze the GEX context at order time (for the audit + Discord + forward-testing).
            indicators.update(self._freeze_gex_context(spot, direction))
            logger.info(f"[{symbol}] GEX SIGNAL: {reason} (entry-vol {indicators.get('iv_entry', 'n/a')})")
        elif reason:                          # an OR breakout formed but regime/momentum blocked it
            self._alert_blocked('gex', symbol, reason)
        return direction, reason, indicators

    def _freeze_gex_context(self, spot: float, direction: str) -> dict:
        """Snapshot the dealer-gamma context at order time → the audit/Discord dict fragment
        (Gflip, distance-to-flip, net GEX total+0DTE, the top-3 support/resistance ladders, and
        the H3 `Setup_Tag` bucket). Shared by the mechanical GEX entry AND thesis trades so both
        book the same columns. Returns {} when no chain is cached (thesis can fire chain-less)."""
        if not self._gex_chain:
            return {}
        gflip = gex.gamma_flip(self._gex_chain, spot)
        calls, puts = gex.gex_ladders(spot, self._gex_chain, n=3)
        ind = {
            'gflip': round(gflip, 2) if gflip else None,
            'dist_gflip_pct': round((spot - gflip) / gflip * 100, 3) if gflip else None,
            'net_gex_total': round(gex.net_gex(spot, self._gex_chain) / 1e6, 0),
            'net_gex_0dte': round(gex.net_gex_0dte(spot, self._gex_chain) / 1e6, 0),
            'call_ladder': "|".join(f"{k:.0f}" for k, _ in calls),   # resistance, heaviest first
            'put_ladder': "|".join(f"{k:.0f}" for k, _ in puts),     # support, heaviest first
            'call_ladder_full': [(k, round(v / 1e6, 0)) for k, v in calls],
            'put_ladder_full': [(k, round(v / 1e6, 0)) for k, v in puts],
        }
        # Setup bucket (docs/GEX_NOTES.md H3): did we enter with runway to the lead
        # support/resistance strike, or into it? A PUT wants heavy support BELOW spot;
        # a CALL wants heavy resistance ABOVE. Auto-tag so trades bucket themselves.
        lead = (puts[0][0] if puts else None) if direction == 'PUT' else (calls[0][0] if calls else None)
        ind['setup_tag'] = ('' if lead is None else
                            ('Runway' if (lead < spot) == (direction == 'PUT') else 'IntoWall'))
        return ind

    def _gex_exit_check(self, symbol: str, trade: dict, profit_pct: float):
        """GEX exits (2026-08-17 — LET THE CONVEX TAIL RIDE): no invalidation cut, no fixed
        max-loss stop. Exit only via (1) a trailing stop that ARMS once the trade peaks at
        GEX_TRAIL_TRIGGER, then exits on giving back GEX_TRAIL_GIVEBACK of the peak, and
        (2) a WIDE catastrophe backstop (GEX_CATASTROPHE_STOP) so a trade that never peaks
        can't ride to a full-premium loss. EOD 3:55 flatten → main loop.
        Rationale: the removed invalidation + 50% stop cut the 08-17 winner at -4% before it
        ran to +100% (max_profit_pct is updated by the caller before this runs)."""
        peak = trade.get('max_profit_pct', 0.0)
        # Trailing stop — only protects gains AFTER the trade has actually run
        if peak >= config.GEX_TRAIL_TRIGGER:
            trail_exit = peak * (1 - config.GEX_TRAIL_GIVEBACK)
            if profit_pct <= trail_exit:
                return True, (f"Trailing stop: peaked +{peak*100:.0f}%, gave back to "
                              f"{profit_pct*100:+.0f}% (trail +{trail_exit*100:.0f}%)")
        # Catastrophe backstop — the only downside floor now (trail can't arm on a loser)
        if config.GEX_CATASTROPHE_STOP > 0 and profit_pct <= -config.GEX_CATASTROPHE_STOP:
            return True, f"Catastrophe stop: lost {abs(profit_pct)*100:.0f}%"
        # Optional hard TP (disabled by default: GEX_TAKE_PROFIT=0 — the tail is the edge)
        if config.GEX_TAKE_PROFIT > 0 and profit_pct >= config.GEX_TAKE_PROFIT:
            return True, f"Take-profit +{config.GEX_TAKE_PROFIT*100:.0f}% hit"
        return False, ""

    # ── Thesis-GEX command rail (#44) ─────────────────────────────────────────
    # A human-authorised thesis, dropped as a JSON command file (see src/commands.py), becomes
    # an armed order the bot watches and fires as a single-leg 'thesis:SPX' trade. Files are the
    # source of truth: pending arms/close-ifs are rebuilt from them each loop (so a restart
    # resumes them) and moved to processed/ once fired/closed/expired/cancelled. Thesis trades
    # reuse ALL account guards + the GEX-style convex-tail exits (routed in the exit dispatcher).

    def _process_thesis_commands(self):
        """Scan the command dir, reject malformed files, refresh the pending arm/close-if lists
        from what's there, announce newly-seen commands once, then run one-shot close/cancel."""
        if not config.THESIS_ENABLED:
            return
        arms, closers, oneshots = [], [], []
        for c in commands.scan(self._thesis_dir):
            ok, err = commands.validate(c)
            if not ok:
                self._thesis_reject(c, err)
                continue
            kind = c['cmd']
            if kind == 'arm':
                arms.append(c)
            elif kind == 'close_if':
                closers.append(c)
            else:                                   # close, cancel — one-shot
                oneshots.append(c)
        self._thesis_arms, self._thesis_closers = arms, closers

        for c in arms + closers:                    # confirm each new arm/close-if once
            if c['id'] not in self._thesis_seen:
                self._thesis_seen.add(c['id'])
                sym = c.get('symbol', config.SYMBOLS[0])
                notifier.notify_thesis_action('armed', sym, self._tkey('thesis', sym),
                                              commands.describe(c))
                logger.info(f"[thesis] {commands.describe(c)} (id={c['id']})")

        for c in oneshots:
            if c['cmd'] == 'close':
                self._thesis_close_now(c)
            elif c['cmd'] == 'cancel':
                self._thesis_cancel(c)

    def _watch_thesis_triggers(self):
        """Evaluate pending arms + close-ifs against the latest spot (and 1-min closes for
        confirmation). Fire arms whose trigger is met, expire the stale ones, and close the
        thesis position when a close-if condition trips. Fetches bars only when something is
        actually pending — zero cost when the rail is idle."""
        if not config.THESIS_ENABLED or (not self._thesis_arms and not self._thesis_closers):
            return
        now = market_time.now_et()
        default_sym = config.SYMBOLS[0]
        for symbol in config.SYMBOLS:
            arms = [a for a in self._thesis_arms if a.get('symbol', default_sym) == symbol]
            closers = [c for c in self._thesis_closers if c.get('symbol', default_sym) == symbol]
            # Expire without needing a market fetch
            for arm in list(arms):
                if commands.is_expired(arm, now):
                    self._thesis_consume(arm, 'expired', 'trigger not met before expires_at')
                    notifier.notify_thesis_action('expired', symbol, self._tkey('thesis', symbol),
                                                  commands.describe(arm))
                    arms.remove(arm)
            for closer in list(closers):
                if commands.is_expired(closer, now):
                    self._thesis_consume(closer, 'expired', 'condition not met before expires_at')
                    closers.remove(closer)
            if not arms and not closers:
                continue
            df = self.broker.fetch_intraday_data(symbol)
            if df is None or df.empty:
                continue
            spot = float(df['close'].iloc[-1])
            recent = [float(x) for x in df['close'].tolist()[-10:]]
            for arm in list(arms):
                or_levels = self._arm_or_levels(arm, df, now)
                if commands.arm_should_fire(arm, spot, recent, or_levels=or_levels):
                    self._fire_thesis_arm(symbol, arm, spot)
            key = self._tkey('thesis', symbol)
            for closer in list(closers):
                trade = self.active_trades.get(key)
                if not trade or trade.get('status') != 'ACTIVE':
                    continue                        # nothing open to protect yet — keep it pending
                if commands.closer_should_fire(closer, spot):
                    reason = f"THESIS close_if: {commands.describe(closer)}"
                    logger.info(f"[thesis] close_if fired (spot {spot:.1f}) — {reason}")
                    self.close_position(key, self._current_value(symbol, trade), reason)
                    self._thesis_consume(closer, 'closed', reason)
                    notifier.notify_thesis_action('close', symbol, key, reason)

    def _arm_or_levels(self, arm: dict, df, now):
        """For an `or_breakout` arm, return the (OR_high, OR_low) of the 15-min opening range —
        computed exactly like the mechanical GEX entry (strategy.opening_range_levels). Returns
        None for any other trigger, OR while the opening-range window is still forming (before
        9:30+or_minutes), OR if there are no bars in the window — so the arm can't fire early."""
        trig = arm.get('trigger') or {}
        if trig.get('type') != 'or_breakout':
            return None
        minutes = int(trig.get('or_minutes', config.GEX_OR_MINUTES))
        open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now < open_t + datetime.timedelta(minutes=minutes):
            return None                              # opening range not complete yet
        return strategy.opening_range_levels(df, now, minutes)

    def _fire_thesis_arm(self, symbol: str, arm: dict, spot: float):
        """A trigger fired: place the single-leg thesis trade via the normal path (all account
        guards + ATM strike + tick-snapped limit apply). Fire-once — the arm is consumed whatever
        the outcome, so a guard block or a re-crossing level can't loop; the user re-arms if needed."""
        key = self._tkey('thesis', symbol)
        side = arm['side']
        if key in self.active_trades:
            self._thesis_consume(arm, 'skipped', 'already holding a thesis position')
            return
        if (self.circuit_breaker_tripped or self.daily_loss_limit_hit
                or self.daily_trade_count >= config.MAX_TRADES_PER_DAY):
            self._thesis_consume(arm, 'blocked', 'account risk guard active (breaker/daily-loss/trade-cap)')
            notifier.notify_thesis_action('blocked', symbol, key, 'account risk guard active')
            return
        reason = f"THESIS: {arm.get('note') or arm.get('id')}"
        indicators = self._thesis_indicators(symbol, spot, side, arm)
        self.execute_trade(symbol, side, reason, indicators, 'thesis')
        fired = key in self.active_trades
        self._thesis_consume(arm, 'fired' if fired else 'blocked', reason)
        if fired:
            logger.info(f"[thesis] ARMED ORDER FIRED: {side} {symbol} @spot {spot:.1f} — {reason}")
            notifier.notify_thesis_action('fired', symbol, key, f"{side} — {commands.describe(arm)}")
        else:
            logger.warning(f"[thesis] arm {arm.get('id')} fired but no position opened "
                           f"(guard/quote) — see log.")
            notifier.notify_thesis_action('blocked', symbol, key, commands.describe(arm))

    def _thesis_close_now(self, cmd: dict):
        """One-shot `close`: close the target thesis position now if it's ACTIVE."""
        symbol = cmd.get('symbol', config.SYMBOLS[0])
        key = cmd.get('target', self._tkey('thesis', symbol))
        trade = self.active_trades.get(key)
        if trade and trade.get('status') == 'ACTIVE':
            reason = f"THESIS close: {cmd.get('note') or cmd.get('id')}"
            self.close_position(key, self._current_value(symbol, trade), reason)
            self._thesis_mark(cmd, 'closed', reason)
            notifier.notify_thesis_action('close', symbol, key, reason)
        else:
            self._thesis_mark(cmd, 'noop', f"no ACTIVE {key} to close")

    def _thesis_cancel(self, cmd: dict):
        """One-shot `cancel`: drop a pending arm/close-if by id (moves its file to processed/)."""
        target = cmd.get('cancel_id')
        found = False
        for lst in (self._thesis_arms, self._thesis_closers):
            for pending in list(lst):
                if pending.get('id') == target:
                    self._thesis_mark(pending, 'cancelled', f"by {cmd.get('id')}")
                    lst.remove(pending)
                    found = True
        self._thesis_mark(cmd, 'done' if found else 'noop',
                          f"cancel {target}{'' if found else ' — not found'}")

    def _thesis_indicators(self, symbol: str, spot: float, side: str, arm: dict) -> dict:
        """Audit/Discord indicator dict for a thesis trade: the human note + the frozen GEX
        context (so thesis trades book the same Gflip/ladder/Setup_Tag columns as GEX). Refreshes
        the chain if none is cached so a thesis trade is never GEX-blind when a chain is available."""
        if not self._gex_chain:
            try:
                self._refresh_gex_chain(symbol)
            except Exception as e:
                logger.warning(f"[thesis] GEX chain refresh failed (trade proceeds context-less): {e}")
        ind = {'current_price': round(spot, 2), 'thesis_note': arm.get('note', '')}
        ind.update(self._freeze_gex_context(spot, side))
        return ind

    def _thesis_consume(self, cmd: dict, status: str, detail: str = ''):
        """Mark a command processed AND drop it from the in-memory pending lists (so the
        same loop's watcher can't act on it again)."""
        self._thesis_mark(cmd, status, detail)
        for lst in (self._thesis_arms, self._thesis_closers):
            if cmd in lst:
                lst.remove(cmd)

    def _thesis_mark(self, cmd: dict, status: str, detail: str = ''):
        dest = commands.mark_processed(cmd, self._thesis_dir, status)
        logger.info(f"[thesis] command {cmd.get('id')} → {status}"
                    f"{': ' + detail if detail else ''}"
                    f"{' (' + os.path.basename(dest) + ')' if dest else ''}")

    def _thesis_reject(self, cmd: dict, err: str):
        logger.warning(f"[thesis] rejecting command {cmd.get('id')}: {err}")
        notifier.notify_thesis_action('rejected', cmd.get('symbol', config.SYMBOLS[0]),
                                      cmd.get('id', '?'), err)
        self._thesis_mark(cmd, 'rejected', err)

    def execute_trade(self, symbol: str, direction: str, reason: str, indicators: dict,
                      strategy: str = 'trend'):
        """Entry guards, then submit a single-leg directional order for the strategy
        (tracked under 'strategy:symbol' so trend and gex don't collide)."""
        if self._tkey(strategy, symbol) in self.active_trades:
            logger.warning(f"Already in active {strategy} trade for {symbol}. Skipping.")
            return

        # Anti-cascade guard: never open on top of an untracked position in this
        # symbol (the 2026-07-09 duplicate cascade). Alert once, don't trade.
        if self._symbol_has_untracked_position(symbol):
            logger.warning(f"[{symbol}] Untracked option position held in account — skipping entry to avoid stacking.")
            self._alert_untracked_once(symbol)
            return

        now_est = market_time.now_et()
        cooldown_expires = self.signal_cooldowns.get((strategy, symbol, direction))
        if cooldown_expires and now_est < cooldown_expires:
            remaining = int((cooldown_expires - now_est).total_seconds() // 60)
            logger.info(f"Signal ({strategy}, {symbol}, {direction}) in cooldown for {remaining}m more. Skipping.")
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
        # Buy ONE ATM (~50Δ) option — CALL bullish, PUT bearish. Single leg.
        self._place_single_leg(symbol, direction, atm_strike, indicators, reason, strategy)

    def _place_single_leg(self, symbol: str, direction: str, strike: float,
                          indicators: dict, reason: str, strategy: str = 'trend'):
        """Buy ONE ATM directional option (structure='SINGLE'). Single long leg.
        Priced mid + ENTRY_AGGRESSION×(ask−mid); fixed 1 contract; no resting TP. Tracked
        under 'strategy:symbol' so trend and gex can each hold one."""
        try:
            opt = self.broker.get_option_contract(symbol, direction, strike)
            bid, mid, ask = self.broker._get_option_quote(opt)
            if mid <= 0:
                logger.warning(f"[{symbol}] No valid quote for single-leg {direction} {strike:g}. Aborting.")
                return
            if mid < config.MIN_OPTION_COST:
                logger.warning(f"[{symbol}] Option mid ${mid:.2f} below min ${config.MIN_OPTION_COST:.2f}. Skipping.")
                return
            raw_limit = mid + config.ENTRY_AGGRESSION * max(ask - mid, 0.0)
            # Snap BOTH the limit and the mid-floor to the price-aware tick — a
            # $0.05 price ≥ $3 → IBKR error 110 (rejected). See broker.snap_to_tick.
            limit_price = max(self.broker.snap_to_tick(symbol, raw_limit),
                              self.broker.snap_to_tick(symbol, mid))

            ibkr_trade = self.broker.place_limit(opt, 'BUY', 1, limit_price)
            self.active_trades[self._tkey(strategy, symbol)] = {
                'strategy': strategy, 'symbol': symbol,
                'structure': 'SINGLE',
                'direction': direction,
                'target_entry_price': limit_price,
                'status': 'PENDING_ENTRY',
                'submitted_at': market_time.now_et(),   # #34 entry-timeout anchor
                'ibkr_trade': ibkr_trade,
                'option_contract': opt,                  # close + value use this
                'qty': 1,
                'max_profit_pct': 0.0,
                'max_adverse_pct': 0.0,   # max adverse excursion (MAE) — deepest drawdown of the hold
                'long_strike': strike,
                'long_conid': opt.conId,                 # reconciliation reference leg
                'leg_conids': [opt.conId],
                'entry_indicators': indicators,
                'reason': reason,
            }
            logger.info(
                f"SUBMITTED {direction} SINGLE-LEG ORDER to IBKR: 1x {symbol} {strike:g} "
                f"at LIMIT ${limit_price:.2f} (mid ${mid:.2f}, ask ${ask:.2f}; "
                f"OrderId: {ibkr_trade.order.orderId})"
            )
            notifier.notify_submit(symbol, direction, strike, strike, limit_price, 1,
                                   config.MAX_POSITION_SIZE, indicators, reason,
                                   ibkr_trade.order.orderId)
            notifier.notify_today_summary(self.active_trades, self.closed_trades_today)
        except Exception as e:
            logger.error(f"[{symbol}] Failed to place single-leg order: {e}")

    def evaluate_exit_conditions(self):
        for symbol in list(self.active_trades.keys()):
            self.evaluate_exit_conditions_for_symbol(symbol)

    def evaluate_exit_conditions_for_symbol(self, key: str):
        if key not in self.active_trades:
            return

        trade = self.active_trades[key]
        # `key` is the strategy:symbol tracking key; `symbol` is the real broker symbol.
        # (So all the broker/log code below is unchanged; only active_trades ops use `key`.)
        symbol = trade.get('symbol', key)

        # A closing order is in flight — confirm its fill before anything else
        if trade.get('status') == 'PENDING_EXIT':
            self._check_pending_exit(key, trade)
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
                    notifier.notify_closed_externally(symbol, trade['direction'])
                    self.active_trades.pop(key, None)
                else:
                    logger.info(f"[{symbol}] Legs still absent ({missing_for:.0f}/{self._RECONCILE_DROP_AFTER_S}s); re-checking.")
                return  # skip exit eval while the position's status is uncertain

        current_value = self._current_value(symbol, trade)
        if current_value <= 0:
            logger.warning(f"Could not fetch option value for {symbol}. Skipping exit eval.")
            return

        if trade.get('status') == 'PENDING_ENTRY':
            self._check_pending_fill(key, trade)
            return

        # single long option: profit = (value − entry) / entry
        profit_pct = self._side(trade) * (current_value - trade['entry_price']) / trade['entry_price']

        # Cache live P&L so the "today" summary can read it without extra IBKR calls
        trade['current_value'] = current_value
        trade['current_profit_pct'] = profit_pct

        if profit_pct > trade['max_profit_pct']:
            trade['max_profit_pct'] = profit_pct
            if profit_pct > 0:
                logger.info(f"[{symbol}] New Max Profit: {profit_pct*100:.2f}%")
        # Max adverse excursion (MAE) — the deepest drawdown seen during the hold, the
        # mirror of max_profit_pct. Recorded on the exit row so we can see how far a
        # winner dipped before it paid (e.g. 08-20's +$810 sat ~-55% for ~3h first).
        if profit_pct < trade.get('max_adverse_pct', 0.0):
            trade['max_adverse_pct'] = profit_pct
            logger.info(f"[{symbol}] New Max Adverse: {profit_pct*100:.2f}%")

        if trade.get('strategy') == 'trend':
            # Trend exits: hard stop + Supertrend reversal (EOD flatten by the main loop).
            exit_triggered, exit_reason = self._trend_exit_check(symbol, trade, profit_pct)
        else:
            # GEX *and* thesis exits: trailing stop + wide catastrophe backstop (EOD flatten by
            # the loop). Thesis trades additionally honour any `close`/`close_if` command.
            exit_triggered, exit_reason = self._gex_exit_check(symbol, trade, profit_pct)
        if exit_triggered:
            logger.info(f"[{symbol}] EXIT TRIGGERED: {exit_reason}")
            self.close_position(key, current_value, exit_reason)

    def _trend_exit_check(self, symbol: str, trade: dict, profit_pct: float):
        """Trend-mode exits: hard stop OR Supertrend reversal (EOD flatten by the
        main loop). The Supertrend check caches its bar fetch ~50s so fast-polling
        doesn't refetch."""
        if profit_pct <= -config.HARD_STOP_LOSS_PCT:
            return True, f"Hard stop loss: lost {abs(profit_pct)*100:.1f}% of entry value"

        reversed_ = trade.get('_trend_rev_last', False)
        checked_at = trade.get('_trend_checked_at')
        if checked_at is None or (market_time.now_et() - checked_at).total_seconds() >= 50:
            try:
                df = self.broker.fetch_intraday_data(symbol)   # trend needs only HLC → SPX direct
                reversed_ = strategy.trend_reversed(trade['direction'], df)
            except Exception as e:
                logger.warning(f"[{symbol}] Trend-reversal check failed: {e}")
            trade['_trend_checked_at'] = market_time.now_et()
            trade['_trend_rev_last'] = reversed_

        if reversed_:
            return True, (f"Trend reversed: Supertrend flipped against the "
                          f"{trade['direction']} (P&L {profit_pct*100:+.1f}%)")
        return False, ""

    @staticmethod
    def _filled_qty(ibkr_trade) -> int:
        """Contracts actually filled on an order (0 when unknown/none)."""
        try:
            return int(float(ibkr_trade.orderStatus.filled or 0))
        except (TypeError, ValueError):
            return 0

    def _expire_stale_entry(self, symbol: str, trade: dict, ibkr_trade) -> bool:
        """#34 — an entry limit outlives its signal.

        A resting limit only fills once the option decays to our bid — i.e. once the
        market has already moved AGAINST the thesis (adverse selection). 2026-07-15:
        an order sat 1h42m, filled, and invalidated 65s later for −$175. The signal's
        shelf life is bars, not hours: cancel and let it be re-evaluated fresh.

        Returns True if the order was expired (caller must stop). A partial fill is a
        REAL position — promote it, never orphan it (#21/#30).
        """
        timeout = config.ENTRY_ORDER_TIMEOUT_SECONDS
        submitted = trade.get('submitted_at')
        if timeout <= 0 or submitted is None:
            return False
        waited = (market_time.now_et() - submitted).total_seconds()
        if waited < timeout:
            return False

        logger.warning(f"[{symbol}] Entry order unfilled after {waited:.0f}s "
                       f"(limit {timeout}s) — signal is stale; cancelling.")
        try:
            if not ibkr_trade.isDone():
                self.broker.cancel_order(ibkr_trade.order)
                self.broker.sleep(1)   # let the cancel/fill race resolve (#30)
        except Exception as e:
            logger.warning(f"[{symbol}] Could not cancel stale entry order: {e}")

        filled_qty = self._filled_qty(ibkr_trade)
        if filled_qty > 0:
            logger.warning(f"[{symbol}] Stale entry PARTIALLY filled {filled_qty}/"
                           f"{trade.get('qty')} before the cancel landed — tracking "
                           f"the live slice rather than orphaning it.")
            trade['qty'] = filled_qty
            self._activate_entry(symbol, trade, ibkr_trade)
            return True

        self.active_trades.pop(symbol, None)
        notifier.notify_entry_expired(symbol, trade, waited)
        return True

    def _check_pending_fill(self, symbol: str, trade: dict):
        """Poll a PENDING_ENTRY order: promote to ACTIVE on fill, drop on cancel,
        expire it once the signal has gone stale (#34)."""
        try:
            self.broker.sleep(0)  # flush event loop so orderStatus is current
            ibkr_trade = trade['ibkr_trade']
            status = ibkr_trade.orderStatus.status

            if status == 'Filled':
                self._activate_entry(symbol, trade, ibkr_trade)

            elif status in ('Cancelled', 'ApiCancelled', 'Inactive'):
                # A cancel can race a fill (#30) — never drop without checking fills.
                filled_qty = self._filled_qty(ibkr_trade)
                if filled_qty > 0:
                    logger.warning(f"[{symbol}] Entry order {status} but {filled_qty} "
                                   f"contract(s) FILLED — keeping the live slice tracked.")
                    trade['qty'] = filled_qty
                    self._activate_entry(symbol, trade, ibkr_trade)
                    return
                logger.warning(f"[{symbol}] IBKR order {status}. Removing from tracking.")
                self.active_trades.pop(symbol, None)
            else:
                if self._expire_stale_entry(symbol, trade, ibkr_trade):
                    return
                logger.info(f"[{symbol}] IBKR order still pending (Status: {status}).")
        except Exception as e:
            logger.error(f"[{symbol}] Error checking IBKR order status: {e}")

    def _activate_entry(self, key: str, trade: dict, ibkr_trade):
        """Promote a filled entry to ACTIVE: park the take-profit, audit, notify.
        Shared by the clean-fill path and the partial-fill rescues."""
        try:
            symbol = trade.get('symbol', key)                # real broker symbol
            strat = trade.get('strategy', 'trend')
            filled_price = float(ibkr_trade.orderStatus.avgFillPrice)
            logger.info(f"[{symbol}] IBKR ORDER FILLED at ${filled_price:.2f}")
            trade['status'] = 'ACTIVE'
            trade['entry_price'] = filled_price
            # Fill time — reconciliation grace period anchor
            trade['activated_at'] = market_time.now_et()
            # permId — the permanent, account-wide key for this trade
            trade['entry_permId'] = self.broker.order_perm_id(ibkr_trade)

            # Cool down + count the trade on the FILL (08-07), not at submission — a
            # timed-out entry took no position, so it must not lock the signal out or
            # burn a daily slot. Guarded so an idempotent re-call can't double-count.
            if not trade.get('_counted'):
                trade['_counted'] = True
                self.daily_trade_count += 1
                cd = market_time.now_et() + datetime.timedelta(minutes=config.SIGNAL_COOLDOWN_MINUTES)
                self.signal_cooldowns[(strat, symbol, trade['direction'])] = cd
                logger.info(f"[{symbol}] Filled — {trade['direction']} cooling down until "
                            f"{cd.strftime('%H:%M')} ET; day trade #{self.daily_trade_count}.")

            # Single-leg runs NO resting take-profit — the convex tail IS the edge.

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
                strategy=strat,
                gflip=ind.get('gflip'), dist_gflip_pct=ind.get('dist_gflip_pct'),
                net_gex_total=ind.get('net_gex_total'), net_gex_0dte=ind.get('net_gex_0dte'),
                call_ladder=ind.get('call_ladder'), put_ladder=ind.get('put_ladder'),
                setup_tag=ind.get('setup_tag'),
            )
            notifier.notify_filled(symbol, trade, filled_price)
        except Exception as e:
            logger.error(f"[{symbol}] Error activating filled entry: {e}")

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
        sym = trade.get('symbol', symbol)
        strat = trade.get('strategy', 'trend')
        fill_price = float(exit_trade.orderStatus.avgFillPrice)
        side = self._side(trade)
        profit_pct = side * (fill_price - trade['entry_price']) / trade['entry_price']
        dollar_pnl = side * (fill_price - trade['entry_price']) * filled_qty * 100
        commission = self.broker.order_commission(exit_trade)
        reason = f"{trade.get('exit_reason', '')} [PARTIAL {filled_qty}/{trade['qty']} before order died]"
        logger.warning(f"[{sym}] Closing order died after partial fill "
                       f"{filled_qty}/{trade['qty']} at ${fill_price:.2f} — booking the slice.")
        self.closed_trades_today.append({
            'symbol': sym, 'strategy': strat, 'direction': trade['direction'],
            'entry_price': trade['entry_price'], 'exit_price': fill_price,
            'profit_pct': profit_pct, 'dollar_pnl': dollar_pnl, 'reason': reason,
            'commission': commission,
        })
        audit.record(
            "SELL", sym, trade['direction'], fill_price, reason,
            profit_pct=profit_pct, dollar_pnl=dollar_pnl,
            peak_pct=trade.get('max_profit_pct'),
            max_adverse_pct=trade.get('max_adverse_pct'), commission=commission,
            perm_id=self.broker.order_perm_id(exit_trade),
            strategy=strat,
        )
        notifier.notify_closed(sym, trade, fill_price, profit_pct, dollar_pnl,
                               reason, commission=commission)
        trade['qty'] -= filled_qty

    def _remaining_qty(self, trade: dict):
        """How much of this position the account ACTUALLY still holds, measured
        on the single long leg. None = unknown (feed unavailable) → caller must defer, not act.
        0 = gone. Negative = OVER-CLOSED (inverse position — critical)."""
        pos = self.broker.position_qty(trade.get('long_conid'))
        return None if pos is None else int(pos)

    # Reprice an unfilled closing order after this many seconds
    _EXIT_REPRICE_AFTER_S = 180
    # Break the reject/retry loop (TODO #26): space out close attempts, and
    # after this many rejections stop auto-retrying and alert a human.
    _MAX_CLOSE_ATTEMPTS = 4
    _CLOSE_RETRY_COOLDOWN_S = 30

    def close_position(self, key: str, current_value: float, reason: str):
        """Submit a closing order and mark the trade PENDING_EXIT.

        P&L is NOT booked here — it's booked in _check_pending_exit once IBKR
        confirms the fill, using the actual fill price and reported commissions
        (the audit previously assumed the submission price; see TODO #14)."""
        if key not in self.active_trades:
            return
        trade = self.active_trades[key]
        symbol = trade.get('symbol', key)                    # real broker symbol
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
                self._book_exit_fill(key, trade, prior)
            else:
                logger.warning(f"[{symbol}] Nothing left to close in the account — treating as externally closed.")
                notifier.notify_closed_externally(symbol, trade.get('direction', ''))
                self.active_trades.pop(key, None)
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

            # A single long leg closes by SELLing its own option contract.
            close_action = 'SELL'
            close_contract = trade['option_contract']
            # Snap the close limit to the valid tick — a $0.05 price ≥ $3 is rejected
            # by IBKR error 110, which blocked EVERY close on 2026-08-17 (the bot
            # could not exit all day). See broker.snap_to_tick / #SL-EXEC.
            close_limit = self.broker.snap_to_tick(symbol, current_value)
            exit_ibkr_trade = self.broker.place_limit(
                close_contract, close_action, trade['qty'], close_limit
            )
            trade['status'] = 'PENDING_EXIT'
            trade['exit_ibkr_trade'] = exit_ibkr_trade
            trade['exit_reason'] = reason
            trade['exit_limit'] = close_limit
            trade['exit_submitted_at'] = market_time.now_et()
            trade['close_attempts'] = attempts + 1
            trade['last_close_attempt_at'] = market_time.now_et()
            logger.info(
                f"[{symbol}] SUBMITTED CLOSING ORDER to IBKR at LIMIT "
                f"${close_limit:.2f} (OrderId: {exit_ibkr_trade.order.orderId}, "
                f"attempt {attempts + 1}/{self._MAX_CLOSE_ATTEMPTS}). Awaiting fill confirmation."
            )
        except Exception as e:
            logger.error(f"[{symbol}] Failed to submit IBKR closing order: {e}")

    def _check_pending_exit(self, symbol: str, trade: dict):
        """Poll a PENDING_EXIT order: book the trade on fill (actual price +
        commissions); reprice if it sits unfilled too long; revert to ACTIVE if
        the order dies so the exit rules can fire again. `symbol` holds the tracking
        key; `sym` is the real broker symbol."""
        sym = trade.get('symbol', symbol)
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
                    root = self.broker.option_symbol(sym)
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
                    fresh = self._current_value(sym, trade)
                    if fresh > 0:
                        fresh = self.broker.snap_to_tick(sym, fresh)   # off-tick → IBKR error 110
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
        audit row — all from the ACTUAL fill price and IBKR-reported commissions.
        `symbol` holds the tracking key; `sym`/`strat` are the real symbol + strategy."""
        sym = trade.get('symbol', symbol)
        strat = trade.get('strategy', 'trend')
        reason = trade.get('exit_reason', '')
        side = self._side(trade)   # +1 — single long option
        profit_pct = side * (fill_price - trade['entry_price']) / trade['entry_price']
        dollar_pnl = side * (fill_price - trade['entry_price']) * trade['qty'] * 100
        logger.info(
            f"[{symbol}] CLOSING ORDER FILLED at ${fill_price:.2f} "
            f"(limit was ${trade.get('exit_limit', 0):.2f}) — P&L {profit_pct*100:+.2f}% "
            f"/ ${dollar_pnl:+.2f}, commissions ${commission:.2f}"
        )

        # Record for the post-close day summary
        self.closed_trades_today.append({
            'symbol': sym, 'strategy': strat, 'direction': trade['direction'],
            'entry_price': trade['entry_price'], 'exit_price': fill_price,
            'profit_pct': profit_pct, 'dollar_pnl': dollar_pnl, 'reason': reason,
            'commission': commission,
        })

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

        notifier.notify_closed(sym, trade, fill_price, profit_pct, dollar_pnl,
                               reason, commission=commission)

        # Capture exit-time indicators (ADX/ORB/underlying) on the SELL row from the
        # symbol's OWN bars — same scale as the entry row. (Historically proxied to SPY
        # for a volume-weighted VWAP in the breakout era; trend/gex use HLC only, so we
        # read SPX directly and the SELL underlying_price now matches the BUY row.)
        df = self.broker.fetch_intraday_data(sym)
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
            "SELL", sym, trade['direction'], fill_price, reason,
            adx=exit_indicators.get('adx'), vwap=exit_indicators.get('vwap'),
            orb_high=exit_indicators.get('orb_high'), orb_low=exit_indicators.get('orb_low'),
            underlying_price=exit_indicators.get('current_price'),
            profit_pct=profit_pct, dollar_pnl=dollar_pnl,
            peak_pct=trade.get('max_profit_pct'),
            max_adverse_pct=trade.get('max_adverse_pct'),
            commission=commission,
            perm_id=trade.get('exit_permId'),
            strategy=strat,
        )
        self.active_trades.pop(symbol, None)

    def close_all_positions(self, reason: str):
        """Force-close every open position (end-of-day flatten) — across all strategies."""
        for key in list(self.active_trades.keys()):
            trade = self.active_trades[key]
            sym = trade.get('symbol', key)
            try:
                if trade.get('status') == 'PENDING_ENTRY':
                    # Unfilled order lingering near the close — cancel instead of selling
                    self.broker.cancel_order(trade['ibkr_trade'].order)
                    logger.info(f"[{sym}] EOD: cancelled unfilled entry order.")
                    self.active_trades.pop(key, None)
                    continue
                current_value = self._current_value(sym, trade)
                if current_value <= 0:
                    current_value = 0.01  # still flatten — submit at minimum tick
                self.close_position(key, current_value, reason)
            except Exception as e:
                logger.error(f"[{sym}] EOD flatten failed: {e}")
                self.active_trades.pop(key, None)

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
        logger.info("Starting 0DTE Options Trading Bot (IBKR)...")
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

                # End-of-day flatten — never hold 0DTE positions into the close. GEX
                # flattens earlier (GEX_FLATTEN_TIME, 3:50) per its playbook.
                gex_flatten = 'gex' in config.ACTIVE_STRATEGIES and market_time.past_gex_flatten()
                if (market_time.is_eod_flatten_time() or gex_flatten) and self.active_trades:
                    logger.info("Flatten time reached — closing all open positions.")
                    self.close_all_positions("End of day — flattening 0DTE positions")

                # ALWAYS collect GEX data (chain + Gflip + distance-to-flip), every loop —
                # even while holding a position or outside the entry window. Never go
                # data-blind again (08-17). Data only; makes no trading decision.
                if 'gex' in config.ACTIVE_STRATEGIES:
                    for symbol in config.SYMBOLS:
                        self._collect_gex_data(symbol)

                # Entry scanning stays on a ~60s cadence even when the loop
                # fast-polls exits — no point re-fetching bars every 15s.
                scan_due = (self._last_entry_scan is None or
                            (market_time.now_et() - self._last_entry_scan).total_seconds() >= 55)
                if scan_due:
                    self._last_entry_scan = market_time.now_et()
                    # Run EVERY active strategy each scan (they hold independent positions).
                    if 'trend' in config.ACTIVE_STRATEGIES:
                        self._scan_trend_entries()
                    if 'gex' in config.ACTIVE_STRATEGIES:
                        self._scan_gex_entries()

                # Thesis-GEX command rail (#44): process human-authorised command files and watch
                # armed triggers EVERY loop (responsive), independently of the trend/gex windows.
                # The mechanical scanners above are untouched; this only adds the 'thesis:SPX' slot.
                self._process_thesis_commands()
                self._watch_thesis_triggers()

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
