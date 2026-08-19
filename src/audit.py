"""Trade audit log — appends every fill (entry and exit) to audit.csv."""
import csv
import logging
import os
from typing import Optional

import config
import market_time

logger = logging.getLogger(__name__)

AUDIT_FILE = "audit.csv"

_COLUMNS = [
    "Timestamp", "Strategy", "Action", "Symbol", "Direction", "Price", "Underlying_Price",
    "ADX", "VWAP", "ORB_High", "ORB_Low", "Breadth", "Reason",
    "Profit_Pct", "Dollar_PnL", "ADX_Slope", "Peak_Pct", "Conviction", "Commission",
    "PermId",
    # GEX context frozen at order time (blank for trend trades). Spot = Underlying_Price.
    # Net GEX is our-convention $M (±5%/3-expiry window); walls are the gamma-weighted
    # heaviest resistance/support strikes (see gex.gex_walls), not raw-OI concentration.
    "Gflip", "Dist_Gflip_Pct", "Net_GEX_Total_M", "Net_GEX_0DTE_M", "Call_Wall", "Put_Wall",
]


def record(action: str, symbol: str, direction: str, price: float, reason: str,
           adx: Optional[float] = None, vwap: Optional[float] = None,
           orb_high: Optional[float] = None, orb_low: Optional[float] = None,
           underlying_price: Optional[float] = None, profit_pct: Optional[float] = None,
           dollar_pnl: Optional[float] = None, breadth: Optional[str] = None,
           adx_slope: Optional[float] = None, peak_pct: Optional[float] = None,
           conviction: Optional[str] = None, commission: Optional[float] = None,
           perm_id: Optional[int] = None, strategy: Optional[str] = None,
           gflip: Optional[float] = None, dist_gflip_pct: Optional[float] = None,
           net_gex_total: Optional[float] = None, net_gex_0dte: Optional[float] = None,
           call_wall: Optional[float] = None, put_wall: Optional[float] = None):
    file_exists = os.path.isfile(AUDIT_FILE)
    try:
        with open(AUDIT_FILE, mode='a', newline='') as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow(_COLUMNS)
            writer.writerow([
                # Logged in ET — the timezone the strategy runs in
                market_time.now_et().strftime("%Y-%m-%d %H:%M:%S"),
                # Which strategy generated this trade (breakout / trend / gex) — defaults
                # to the live STRATEGY so GEX trades are distinguishable from trend ones.
                strategy or config.STRATEGY,
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
                f"{peak_pct*100:.2f}%" if peak_pct is not None else "",
                conviction or "",
                f"{commission:.2f}" if commission is not None else "",
                str(perm_id) if perm_id else "",
                f"{gflip:.2f}" if gflip is not None else "",
                f"{dist_gflip_pct:+.3f}%" if dist_gflip_pct is not None else "",
                f"{net_gex_total:.0f}" if net_gex_total is not None else "",
                f"{net_gex_0dte:.0f}" if net_gex_0dte is not None else "",
                f"{call_wall:.0f}" if call_wall is not None else "",
                f"{put_wall:.0f}" if put_wall is not None else "",
            ])
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")
