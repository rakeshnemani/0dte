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


# Cash-settled index underlyings (European exercise → NOT assignable). Anything not
# listed here is treated as a SMART-routed equity/ETF (SPY/QQQ/IWM unchanged).
#   exchange        — where the underlying INDEX prices
#   option_root     — the option trading class / symbol root
#   option_exchange — where the options + combos route (SMART can misqualify these)
# SPX preserved exactly as before (options via SMART). XSP added 2026-07-13 (#3):
# if CBOE qualification fails in paper, SMART is the fallback to try.
INDEX_SPECS = {
    'SPX': {'exchange': 'CBOE', 'option_root': 'SPXW', 'option_exchange': 'SMART'},
    'XSP': {'exchange': 'CBOE', 'option_root': 'XSP',  'option_exchange': 'CBOE'},
}


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
    # Data-farm connectivity transitions (alerted on-change, not on every heartbeat)
    _FARM_DOWN_CODES = {2103: 'market-data', 2105: 'historical-data'}
    _FARM_UP_CODES = {2104: 'market-data', 2106: 'historical-data'}

    def __init__(self):
        self.ib = IB()
        # Data-farm connectivity state → on-change alerts (a dropped feed blinds the
        # bot: no bars → no entries). bot wires on_farm_change to notify_data_farm.
        self._farm_down: set = set()
        self.on_farm_change = None
        # Breadth data cache — TICK and VOLD are market-wide, shared across all
        # symbols. Cache for 60 seconds to avoid redundant IBKR requests per loop.
        self._breadth_cache_time: Optional[datetime.datetime] = None
        self._tick_df_cache: pd.DataFrame = pd.DataFrame()
        self._vold_df_cache: pd.DataFrame = pd.DataFrame()

    # ── Connection ───────────────────────────────────────────────────────────

    def connect(self):
        logger.info(f"Connecting to IBKR at {config.IBKR_HOST}:{config.IBKR_PORT} (clientId={config.IBKR_CLIENT_ID})")
        self.ib.connect(config.IBKR_HOST, config.IBKR_PORT, clientId=config.IBKR_CLIENT_ID)
        # 1 = live (real-time), 3/4 = delayed. Real-time needs data subscriptions;
        # the paper account inherits them from the live account.
        self.ib.reqMarketDataType(config.IBKR_MARKET_DATA_TYPE)
        _dtype = {1: "live/real-time", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}
        logger.info(f"Market data type: {config.IBKR_MARKET_DATA_TYPE} "
                    f"({_dtype.get(config.IBKR_MARKET_DATA_TYPE, 'unknown')})")
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
        self._check_data_farm(errorCode, errorString)
        if errorCode in self._IBKR_INFO_CODES:
            logger.debug(f"IBKR [{errorCode}] reqId={reqId}: {errorString}")
        else:
            logger.warning(f"IBKR error [{errorCode}] reqId={reqId}: {errorString}")

    def _check_data_farm(self, errorCode: int, errorString: str):
        """Fire an on-change alert when a data farm drops (2103/2105) or recovers
        (2104/2106). Transition-based + deduped via _farm_down, so the periodic
        'connection is OK' heartbeats don't spam. A dropped feed blinds the bot
        (no bars → no entries) until it reconnects — 2026-08-12 it was blind ~18 min."""
        def _emit(state, farm):
            if self.on_farm_change:
                try:
                    self.on_farm_change(state, farm, errorString)
                except Exception as e:
                    logger.warning(f"Data-farm alert failed: {e}")
        if errorCode in self._FARM_DOWN_CODES:
            farm = self._FARM_DOWN_CODES[errorCode]
            if farm not in self._farm_down:
                self._farm_down.add(farm)
                logger.warning(f"Data farm DOWN: {farm} — bot is data-blind until it recovers.")
                _emit('down', farm)
        elif errorCode in self._FARM_UP_CODES:
            farm = self._FARM_UP_CODES[errorCode]
            if farm in self._farm_down:
                self._farm_down.discard(farm)
                logger.info(f"Data farm RESTORED: {farm} — evaluation resumes.")
                _emit('up', farm)

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
        spec = INDEX_SPECS.get(symbol)
        if spec:
            return Index(symbol, spec['exchange'], 'USD')
        return Stock(symbol, 'SMART', 'USD')

    def option_symbol(self, symbol: str) -> str:
        """Map 0DTE underlying symbol to the correct IBKR option root (SPX → SPXW)."""
        spec = INDEX_SPECS.get(symbol)
        return spec['option_root'] if spec else symbol

    def option_exchange(self, symbol: str) -> str:
        """Exchange the options + combos route on ('SMART' for equities/ETFs;
        the listing exchange for cash-settled index options like XSP)."""
        spec = INDEX_SPECS.get(symbol)
        return spec['option_exchange'] if spec else 'SMART'

    def get_option_contract(self, symbol: str, direction: str, strike: float) -> Option:
        today_str = market_time.now_et().strftime('%Y%m%d')
        right = 'C' if direction == 'CALL' else 'P'
        root = self.option_symbol(symbol)
        if symbol in INDEX_SPECS:
            # Cash-settled index options need an explicit exchange + trading class;
            # SMART routing can pick the wrong (or no) contract.
            contract = Option(root, today_str, strike, right, self.option_exchange(symbol),
                              tradingClass=root, multiplier='100', currency='USD')
        else:
            contract = Option(root, today_str, strike, right, 'SMART')
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError(f"Cannot qualify option: {root} {today_str} {right} {strike}")
        return qualified[0]

    def make_bag(self, symbol: str, long_conid: int, short_conid: int) -> Contract:
        """Build a 2-leg BAG combo contract (long leg BUY, short leg SELL)."""
        return self.make_bag_multi(symbol, [(long_conid, 'BUY'), (short_conid, 'SELL')])

    def make_bag_multi(self, symbol: str, legs: list) -> Contract:
        """Build an N-leg BAG combo from [(conId, action), ...].

        Convention: define the package so its market value is POSITIVE (each
        vertical's expensive leg gets the BUY action). Then a BUY order opens a
        debit position and a SELL order opens a credit position (e.g. an iron
        condor), and limit prices stay plain positive numbers either way."""
        exch = self.option_exchange(symbol)
        bag = Contract()
        # #41 (07-27): a BAG's symbol is the UNDERLYING, not the option root. For SPX
        # the root is 'SPXW' but the legs are on underlying 'SPX' — using the root got
        # IBKR error 478 ("Requested symbol SPXW, in legs SPX") and every SPX order was
        # rejected. Unchanged for SPY/XSP (root == underlying); fixes SPX.
        bag.symbol = symbol
        bag.secType = 'BAG'
        bag.currency = 'USD'
        bag.exchange = exch
        bag.comboLegs = [ComboLeg(conId=cid, ratio=1, action=action, exchange=exch)
                         for cid, action in legs]
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

    def last_order_error(self, ibkr_trade) -> Tuple[int, str]:
        """Most recent REAL (errorCode, message) from a trade's log — skips
        informational codes (e.g. 10349 "TIF set to DAY", which masked the real
        201 on 2026-07-10). Falls back to the last info code, else (0, '')."""
        fallback = (0, '')
        try:
            for entry in reversed(ibkr_trade.log):
                if entry.errorCode:
                    if entry.errorCode not in self._IBKR_INFO_CODES:
                        return int(entry.errorCode), str(entry.message)
                    if fallback == (0, ''):
                        fallback = (int(entry.errorCode), str(entry.message))
        except Exception:
            pass
        return fallback

    def position_qty(self, conid: int):
        """Signed account position for one conId. Returns None when the answer
        is UNKNOWN (fetch error, or the option-positions feed is empty — which is
        indistinguishable from a glitch); callers must defer, not act, on None."""
        try:
            positions = self.ib.positions()
        except Exception as e:
            logger.warning(f"position_qty fetch failed: {e}")
            return None
        opts = [p for p in positions if p.contract.secType == 'OPT' and p.position != 0]
        if not opts:
            return None
        for p in opts:
            if p.contract.conId == conid:
                return float(p.position)
        return 0.0

    def cancel_open_orders_for(self, symbol_root: str, except_order_id=None) -> int:
        """Cancel every still-open order on an underlying (used to clear the
        conflicting order behind an error-201 rejection). Skips except_order_id."""
        n = 0
        try:
            for t in self.ib.openTrades():
                if getattr(t.contract, 'symbol', None) != symbol_root or t.isDone():
                    continue
                if except_order_id is not None and t.order.orderId == except_order_id:
                    continue
                self.ib.cancelOrder(t.order)
                n += 1
        except Exception as e:
            logger.warning(f"Could not sweep open orders for {symbol_root}: {e}")
        return n

    def order_perm_id(self, ibkr_trade) -> int:
        """IBKR permId for a trade — the permanent, account-wide order key
        (survives restarts; same across API clients). 0 until IBKR acknowledges."""
        try:
            return int(ibkr_trade.order.permId or 0)
        except Exception:
            return 0

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

            # BUG FIX (2026-08-11): indices were requesting MIDPOINT → returns 0 bars
            # (an INDEX has no bid/ask, so no midpoint). reqHistoricalData returns the
            # index LEVEL via 'TRADES'. This 0-bar result made trend/gex bail at the
            # empty-bar check EVERY scan — the bot never evaluated a single entry.
            # Try TRADES first (works for indices AND equities), MIDPOINT as fallback.
            def _hist(what):
                return self.ib.reqHistoricalData(
                    contract, endDateTime='', durationStr='1 D', barSizeSetting='1 min',
                    whatToShow=what, useRTH=True, formatDate=1, timeout=30)
            bars = _hist('TRADES') or _hist('MIDPOINT')
            if not bars:
                logger.warning(f"[{symbol}] Intraday fetch returned no bars (TRADES + MIDPOINT).")
                return pd.DataFrame()

            # Build the datetime index WITHOUT chained-column writes: pandas 2.x
            # flags `df['col'] = …` after a boolean slice (a view) with a
            # ChainedAssignmentError FutureWarning. .assign returns a fresh frame
            # and we .copy() after the slice so the volume write lands on an owned
            # frame. (A bare .copy() at the top does NOT suffice — the flagged
            # write is post-slice. Verified warning-free 2026-08-14.)
            df = util.df(bars)
            dates = pd.to_datetime(df['date'])
            dates = (dates.dt.tz_localize('America/New_York') if dates.dt.tz is None
                     else dates.dt.tz_convert('America/New_York'))
            df = df.assign(date=dates).set_index('date')

            # Keep only today's RTH bars (IBKR durationStr='1 D' can include
            # yesterday's bars; delayed data also backfills with NaN rows)
            now = market_time.now_et()
            df = df[df.index >= market_time.market_open_today()].copy()

            # Drop rows where key price fields are NaN (delayed feed backfill artefacts)
            df = df.dropna(subset=['open', 'high', 'low', 'close'])

            # SPX MIDPOINT bars have no volume; set dummy volume=1 so VWAP math works
            if 'volume' not in df.columns or (df['volume'] == 0).all():
                df = df.assign(volume=1)

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

    def fetch_gex_chain(self, symbol: str, strike_pct: float = 0.05,
                        n_expiries: int = 3, max_strikes: int = 60):
        """Open interest + IV per (strike, expiry) near spot, for the GEX calc. Returns
        (spot, chain) where chain = [{strike, oi_call, oi_put, iv, T}, ...] (one entry per
        strike×expiry, each with its own time-to-expiry). Batched to respect data-line
        limits. OI is once-daily → the caller caches this and recomputes Gflip intraday
        from the cached chain as spot moves. Index (SPX/XSP) only."""
        spec = INDEX_SPECS.get(symbol)
        if not spec:
            return 0.0, []
        try:
            und = self.underlying_contract(symbol)
            self.ib.qualifyContracts(und)
            spot = self._ticker_mid(self._request_snapshot(und))
            if spot <= 0:
                return 0.0, []
            chains = self.ib.reqSecDefOptParams(symbol, '', 'IND', und.conId)
            root = spec['option_root']
            cs = [c for c in chains if c.tradingClass == root] or chains
            if not cs:
                return spot, []
            exps = sorted(e for e in cs[0].expirations)[:n_expiries]
            lo, hi = spot * (1 - strike_pct), spot * (1 + strike_pct)
            strikes = sorted([s for s in cs[0].strikes if lo <= s <= hi],
                             key=lambda s: abs(s - spot))[:max_strikes]
            exch = spec['option_exchange']
            contracts = [Option(root, e, k, r, exch, tradingClass=root, multiplier='100', currency='USD')
                         for e in exps for k in strikes for r in ('C', 'P')]
            qualified = []
            for i in range(0, len(contracts), 50):
                qualified += self.ib.qualifyContracts(*contracts[i:i + 50])

            agg = {}     # (strike, expiry) -> {oi_call, oi_put, iv}
            for i in range(0, len(qualified), 50):
                batch = qualified[i:i + 50]
                tickers = [self.ib.reqMktData(c, '100,101', False, False) for c in batch]
                self.ib.sleep(8)                      # let OI (static) + greeks populate
                for c, t in zip(batch, tickers):
                    key = (float(c.strike), c.lastTradeDateOrContractMonth)
                    d = agg.setdefault(key, {'oi_call': 0.0, 'oi_put': 0.0, 'iv': None})
                    oi = t.callOpenInterest if c.right == 'C' else t.putOpenInterest
                    if oi is not None and oi == oi:    # not NaN
                        d['oi_call' if c.right == 'C' else 'oi_put'] = float(oi)
                    g = t.modelGreeks
                    iv = getattr(g, 'impliedVol', None) if g else None
                    if d['iv'] is None and iv and iv == iv and iv > 0:
                        d['iv'] = float(iv)
                    self.ib.cancelMktData(c)

            now = market_time.now_et()
            chain = []
            for (strike, expiry), d in agg.items():
                if d['oi_call'] == 0 and d['oi_put'] == 0:
                    continue
                exp_dt = market_time.TZ.localize(
                    datetime.datetime.strptime(expiry, '%Y%m%d').replace(hour=16))
                T = max((exp_dt - now).total_seconds(), 60) / (365.25 * 24 * 3600)
                chain.append({'strike': strike, 'oi_call': d['oi_call'], 'oi_put': d['oi_put'],
                              'iv': d['iv'] or 0.15, 'T': T})
            logger.info(f"[{symbol}] GEX chain: {len(chain)} strike-expiries, spot {spot:.2f}, "
                        f"{len(exps)} expiries, ±{strike_pct:.0%}")
            return spot, chain
        except Exception as e:
            logger.error(f"[{symbol}] fetch_gex_chain failed: {e}")
            return 0.0, []

    def _get_option_mid(self, contract: Option) -> float:
        """Fetch the mid-price (bid/ask midpoint) for a single option leg."""
        try:
            ticker = self._request_snapshot(contract)
            return self._ticker_mid(ticker)
        except Exception as e:
            logger.error(f"Error fetching option mid price: {e}")
            return 0.0

    def _get_option_quote(self, contract: Option):
        """(bid, mid, ask) for one option leg from a single snapshot. bid/ask are 0
        when unquoted; mid falls back through last/close (via _ticker_mid)."""
        def valid(v):
            return v is not None and v == v and v > 0
        try:
            ticker = self._request_snapshot(contract)
            bid = float(ticker.bid) if valid(ticker.bid) else 0.0
            ask = float(ticker.ask) if valid(ticker.ask) else 0.0
            return bid, self._ticker_mid(ticker), ask
        except Exception as e:
            logger.error(f"Error fetching option quote: {e}")
            return 0.0, 0.0, 0.0

    def option_tick(self, symbol: str, price: float = None) -> float:
        """Min price increment for a limit on this symbol's options.

        SPX/XSP index options tick $0.05 for premium < $3.00 and $0.10 at/above
        (CBOE variable increment). A $0.05-aligned price ≥ $3 is REJECTED by IBKR
        with error 110 (this cost us the 2026-08-13 GEX trade — limit 11.65 bounced).
        A $0.10-aligned price is valid at ANY premium, so default to $0.10 when the
        price is unknown. Equities/ETFs tick $0.01."""
        if symbol in INDEX_SPECS:
            return 0.05 if (price is not None and price < 3.0) else 0.10
        return 0.01

    def snap_to_tick(self, symbol: str, price: float) -> float:
        """Round a limit price to this symbol's valid (price-aware) option tick.
        An off-tick index-option limit is rejected by IBKR error 110 — this must be
        applied to EVERY option limit (entry AND close). See option_tick."""
        tick = self.option_tick(symbol, price)
        return round(round(price / tick) * tick, 2)

    def get_spread_quote(self, symbol: str, direction: str,
                         long_strike: float, short_strike: float):
        """Net-debit (bid, mid, ask) for the long/short vertical, from ONE snapshot
        of each leg. ask = marketable BUY (pay the long's ask, sell the short's bid);
        bid = the reverse. Collapses to mid-only if a leg lacks a two-sided quote —
        so the caller safely falls back to the old mid behavior. (0,0,0) on failure."""
        try:
            long_c = self.get_option_contract(symbol, direction, long_strike)
            short_c = self.get_option_contract(symbol, direction, short_strike)
            lb, lm, la = self._get_option_quote(long_c)
            sb, sm, sa = self._get_option_quote(short_c)
            if lm <= 0 or sm <= 0:
                logger.warning(f"Could not price {symbol} {direction} {long_strike}/{short_strike}: "
                               f"long_mid={lm} short_mid={sm}")
                return 0.0, 0.0, 0.0
            mid = max(lm - sm, 0.01)
            if la > 0 and sa > 0:                     # both legs tradeable → real ask/bid
                ask = max(la - sb, 0.01)              # buy long @ ask, sell short @ bid
                bid = max(lb - sa, 0.01)              # sell long @ bid, buy short @ ask
            else:
                ask = bid = mid
            return bid, mid, ask
        except Exception as e:
            logger.error(f"Error fetching spread quote for {symbol}: {e}")
            return 0.0, 0.0, 0.0

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

    def get_condor_value(self, symbol: str, short_call: float, wing_call: float,
                         short_put: float, wing_put: float) -> float:
        """Current market value of an iron condor package (positive number).

        Value = (short_call − call_wing) + (short_put − put_wing) mids — i.e.
        the credit collected to open (SELL) or the cost to close (BUY)."""
        try:
            sc = self._get_option_mid(self.get_option_contract(symbol, 'CALL', short_call))
            wc = self._get_option_mid(self.get_option_contract(symbol, 'CALL', wing_call))
            sp = self._get_option_mid(self.get_option_contract(symbol, 'PUT', short_put))
            wp = self._get_option_mid(self.get_option_contract(symbol, 'PUT', wing_put))
            if min(sc, sp) <= 0 or wc < 0 or wp < 0:
                logger.warning(f"Could not price {symbol} condor {short_put}/{short_call}: "
                               f"sc={sc} wc={wc} sp={sp} wp={wp}")
                return 0.0
            return max((sc - wc) + (sp - wp), 0.01)
        except Exception as e:
            logger.error(f"Error fetching condor value for {symbol}: {e}")
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
