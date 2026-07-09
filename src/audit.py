"""Trade audit log — appends every fill (entry and exit) to audit.csv."""
import csv
import logging
import os
from typing import Optional

import market_time

logger = logging.getLogger(__name__)

AUDIT_FILE = "audit.csv"

_COLUMNS = [
    "Timestamp", "Action", "Symbol", "Direction", "Price", "Underlying_Price",
    "ADX", "VWAP", "ORB_High", "ORB_Low", "Breadth", "Reason",
    "Profit_Pct", "Dollar_PnL", "ADX_Slope", "Peak_Pct", "Conviction", "Commission",
    "PermId",
]


def record(action: str, symbol: str, direction: str, price: float, reason: str,
           adx: Optional[float] = None, vwap: Optional[float] = None,
           orb_high: Optional[float] = None, orb_low: Optional[float] = None,
           underlying_price: Optional[float] = None, profit_pct: Optional[float] = None,
           dollar_pnl: Optional[float] = None, breadth: Optional[str] = None,
           adx_slope: Optional[float] = None, peak_pct: Optional[float] = None,
           conviction: Optional[str] = None, commission: Optional[float] = None,
           perm_id: Optional[int] = None):
    file_exists = os.path.isfile(AUDIT_FILE)
    try:
        with open(AUDIT_FILE, mode='a', newline='') as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow(_COLUMNS)
            writer.writerow([
                # Logged in ET — the timezone the strategy runs in
                market_time.now_et().strftime("%Y-%m-%d %H:%M:%S"),
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
            ])
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")
