"""Discord notifications — the send transport plus every message template.

All message content lives here so bot.py stays orchestration-only. Each
notify_* function formats and sends one alert type.
"""
import logging

import requests

import config

logger = logging.getLogger(__name__)

# Embed colors
GREEN = 0x2ECC71
BLUE = 0x3498DB
RED = 0xE74C3C
BRIGHT_RED = 0xFF0000
ORANGE = 0xE67E22
AMBER = 0xF39C12
GREY = 0x95A5A6


def send(title: str, description: str, color: int):
    if not config.DISCORD_WEBHOOK_URL:
        return
    payload = {"embeds": [{"title": title, "description": description, "color": color}]}
    try:
        response = requests.post(config.DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        if response.status_code not in (200, 204):
            logger.error(f"Discord alert failed: HTTP {response.status_code}")
    except Exception as e:
        logger.error(f"Discord alert failed: {e}")


# ── Trade lifecycle alerts ───────────────────────────────────────────────────

def notify_submit(symbol: str, direction: str, long_strike: float, short_strike: float,
                  spread_cost: float, qty: int, budget: float, indicators: dict,
                  reason: str, order_id):
    iv = indicators.get('iv_entry')
    desc = (
        f"**📊 Ticker:** {symbol}\n"
        f"**🎯 Direction:** {direction} (single-leg)\n"
        f"**⚙️ Strike:** ${long_strike:.0f} — 1× long {direction}\n"
        f"**💰 Limit Price:** ${spread_cost:.2f} per contract\n"
        f"**📈 Quantity:** {qty}\n"
        + (f"**🌡️ Entry-vol:** {iv:.3f}\n" if iv is not None else "") +
        f"\n**📝 Signal:** {reason}\n"
        f"**⏳ Status:** Pending fill (Order #{order_id})"
    )
    send("⏳ ORDER SUBMITTED — Awaiting Fill", desc, AMBER)


def notify_filled(symbol: str, trade: dict, filled_price: float):
    desc = (
        f"**📊 Ticker:** {symbol}\n"
        f"**🎯 Direction:** {trade['direction']} (single-leg)\n"
        f"**⚙️ Strike:** ${trade['long_strike']:.0f} — 1× long\n"
        f"**💰 Entry Price:** ${filled_price:.2f} per contract\n"
        f"**📈 Quantity:** {trade['qty']}\n"
        f"**💸 Total:** ${filled_price * trade['qty'] * 100:.2f}\n"
        f"\n**📝 Reason:** {trade.get('reason', 'N/A')}"
    )
    send("🟢 NEW 0DTE ENTRY", desc, GREEN)


def notify_data_farm(state: str, farm: str, msg: str = ''):
    """Alert when an IBKR data farm drops or recovers. A dropped feed blinds the
    bot (no bars → no entries) until it reconnects — on-change only (see
    broker._check_data_farm), so heartbeats don't spam."""
    if state == 'down':
        send("🔌 DATA FEED DOWN — bot is data-blind",
             f"IBKR **{farm}** farm connection dropped. The bot can't fetch bars or "
             f"evaluate entries until it reconnects.\n\n`{msg}`", RED)
    else:
        send("✅ DATA FEED RESTORED",
             f"IBKR **{farm}** farm reconnected — evaluation resumes.\n\n`{msg}`", GREEN)


def notify_closed(symbol: str, trade: dict, exit_price: float,
                  profit_pct: float, dollar_pnl: float, reason: str,
                  commission: float = 0.0):
    desc = (
        f"**📊 Ticker:** {symbol}\n"
        f"**🎯 Direction:** {trade['direction']} Spread\n"
        f"**🚪 Exit Fill:** ${exit_price:.2f} per contract (IBKR-confirmed)\n\n"
        f"**📈 Performance:**\n"
        f"• Net Profit: {profit_pct*100:+.2f}%\n"
        f"• Dollar PnL: ${dollar_pnl:+.2f}\n"
        + (f"• Commissions (round trip): ${commission:.2f} → net ${dollar_pnl - commission:+.2f}\n"
           if commission else "") +
        f"• Max Profit Reached: {trade.get('max_profit_pct', 0)*100:.2f}%\n\n"
        f"**📝 Exit Reason:** {reason}"
    )
    send("🔵 CLOSED 0DTE SPREAD POSITION", desc, BLUE if profit_pct > 0 else RED)


# ── Risk / lifecycle events ──────────────────────────────────────────────────

def notify_over_closed(symbol: str, direction: str, position: float):
    send(
        "🚨 OVER-CLOSED — INVERSE POSITION IN ACCOUNT",
        f"**{symbol} {direction}**: the account position on the tracked leg is "
        f"**{position:+g}** — a prior close executed MORE than the position size, "
        f"leaving an inverse position the bot did not intend.\n"
        f"The bot has **halted all automatic action on {symbol}** — "
        f"**flatten it manually in IBKR now.**",
        BRIGHT_RED
    )


def notify_close_failed(symbol: str, direction: str, attempts: int, code: int, msg: str):
    send(
        "🛑 CLOSE FAILED — MANUAL ACTION NEEDED",
        f"**{symbol} {direction}** could not be closed after {attempts} attempts "
        f"(last IBKR error **{code}**: {msg[:200]}).\n"
        f"The bot has **stopped auto-retrying** to avoid an order loop and is still "
        f"tracking the position — **close it manually in IBKR** if it doesn't expire cleanly.",
        BRIGHT_RED
    )


def notify_circuit_breaker(consecutive_losses: int):
    send(
        "🚨 CIRCUIT BREAKER TRIPPED",
        f"**{consecutive_losses} consecutive losing trades.**\n"
        f"No new entries will be placed for the rest of today.",
        BRIGHT_RED
    )


def notify_daily_loss_limit(realized: float, limit: float):
    send(
        "🛑 DAILY LOSS LIMIT HIT",
        f"**Realized P&L today: ${realized:+.2f}** (net of commissions) breached the "
        f"-${limit:.0f} daily limit.\nNo new entries for the rest of today. "
        f"Open positions remain managed by the exit rules.",
        BRIGHT_RED
    )


def notify_signal_blocked(strategy: str, symbol: str, reason: str):
    """A setup formed but NO trade was placed (a filter blocked it). Surfaced for
    process transparency / confidence — throttled by the bot so it isn't spammy."""
    send(f"⏸️ {strategy.upper()} signal skipped — {symbol}", reason, GREY)


def notify_closed_externally(symbol: str, direction: str):
    send(
        "⚠️ POSITION CLOSED EXTERNALLY",
        f"**{symbol} {direction} Spread** is no longer held in your IBKR "
        f"account — it was closed outside the bot (Client Portal, mobile app, or TWS).\n"
        f"Removed from tracking; the bot will not manage it or record a P&L for it.",
        ORANGE
    )


def notify_entry_expired(symbol: str, trade: dict, waited_s: float):
    send(
        "⌛ ENTRY ORDER EXPIRED (stale signal)",
        f"**{symbol} {trade.get('direction')} Spread** limit @ "
        f"${trade.get('target_entry_price', 0):.2f} went unfilled for "
        f"{waited_s/60:.1f} min and was cancelled — no position opened.\n"
        f"A resting limit only fills once the spread decays to our bid, i.e. once "
        f"the market has moved *against* the thesis. The signal is stale; the bot "
        f"will re-evaluate fresh rather than buy a dead setup (#34).",
        ORANGE
    )


def notify_untracked_holding(symbol: str, lines: list):
    send(
        "⚠️ UNTRACKED POSITION — ENTRIES BLOCKED",
        f"The account holds **{symbol}** option legs that no tracked trade owns "
        f"(a leftover the bot isn't managing). New {symbol} entries are blocked "
        f"until it's cleared, to avoid stacking positions:\n\n" + "\n".join(lines)
        + "\n\n**Flatten it manually in IBKR** (or restart the bot to adopt it).",
        RED
    )


def notify_unadoptable(lines: list):
    send(
        "⚠️ UNTRACKED POSITIONS NEED ATTENTION",
        "These account positions could not be adopted (not a clean 0DTE spread "
        "pair). The bot will NOT manage them — review manually:\n\n" + "\n".join(lines),
        RED
    )


# ── Summaries ────────────────────────────────────────────────────────────────

def notify_today_summary(active_trades: dict, closed_trades: list):
    """Snapshot after every new trade: open positions (live P&L), closed trades,
    running net. Reads cached values only — no market data calls."""
    open_lines = []
    for sym, trade in active_trades.items():
        direction = trade.get('direction', '')
        if trade.get('status') == 'PENDING_ENTRY' or 'current_value' not in trade:
            open_lines.append(f"• {sym} {direction} — pending fill")
            continue
        entry = trade['entry_price']
        cur = trade['current_value']
        pct = trade.get('current_profit_pct', 0) * 100
        peak = trade.get('max_profit_pct', 0) * 100
        open_lines.append(
            f"• {sym} {direction}  ${entry:.2f} → ${cur:.2f}  {pct:+.1f}%  (peak {peak:+.1f}%)"
        )

    closed_lines = []
    net = 0.0
    for c in closed_trades:
        net += c['dollar_pnl']
        closed_lines.append(
            f"• {c['symbol']} {c['direction']}  {c['profit_pct']*100:+.1f}%  ${c['dollar_pnl']:+.2f}"
        )

    desc = f"**▶ OPEN ({len(active_trades)})**\n"
    desc += ("\n".join(open_lines) if open_lines else "_none_") + "\n\n"
    desc += f"**✅ CLOSED ({len(closed_trades)})**\n"
    desc += ("\n".join(closed_lines) if closed_lines else "_none_") + "\n\n"
    desc += f"**💵 Net so far (realized):** ${net:+.2f}"
    send("📋 TODAY", desc, GREEN if net >= 0 else RED)


def notify_hourly_health(active_trades: dict, closed_trades: list):
    """Hourly heartbeat: awaiting-fill / open (live P&L) / closed-today + net.
    A liveness signal that surfaces orphans or stuck orders within the hour."""
    pending = [(s, t) for s, t in active_trades.items()
               if t.get('status') in ('PENDING_ENTRY', 'PENDING_EXIT')]
    open_ = [(s, t) for s, t in active_trades.items() if t.get('status') == 'ACTIVE']
    net = sum(c['dollar_pnl'] for c in closed_trades)

    lines = [f"**⏳ Awaiting fill ({len(pending)})**"]
    lines += ([f"• {s} {t.get('direction', '')} — {t.get('status')}"
               + (" ⚠️ close failed" if t.get('close_failed') else "") for s, t in pending]
              or ["_none_"])
    lines.append(f"\n**▶ Open ({len(open_)})**")
    if open_:
        for s, t in open_:
            pct = t.get('current_profit_pct')
            peak = t.get('max_profit_pct', 0) * 100
            pct_s = f"{pct*100:+.1f}%" if pct is not None else "—"
            lines.append(f"• {s} {t.get('direction', '')}  {pct_s}  (peak {peak:+.1f}%)")
    else:
        lines.append("_none_")
    lines.append(f"\n**✅ Closed today:** {len(closed_trades)}  |  **Net so far:** ${net:+.2f}")
    send("⏰ HOURLY STATUS", "\n".join(lines), GREEN if net >= 0 else RED)


def notify_day_summary(date, closed_trades: list, circuit_breaker_tripped: bool):
    """End-of-day realized P&L summary. No-op when the day had no closed trades."""
    if not closed_trades:
        return
    net = sum(c['dollar_pnl'] for c in closed_trades)
    fees = sum(c.get('commission', 0.0) for c in closed_trades)
    wins = sum(1 for c in closed_trades if c['profit_pct'] > 0)
    losses = sum(1 for c in closed_trades if c['profit_pct'] <= 0)
    win_rate = wins / len(closed_trades) * 100

    lines = [
        f"• {c['symbol']} {c['direction']}  {c['profit_pct']*100:+.1f}%  ${c['dollar_pnl']:+.2f}"
        for c in closed_trades
    ]
    desc = (
        f"**📅 {date}**\n\n"
        f"**💵 Gross P&L:** ${net:+.2f}\n"
        f"**💸 Commissions:** ${fees:.2f}  →  **Net after fees: ${net - fees:+.2f}**\n"
        f"**📊 Trades:** {len(closed_trades)}  |  Wins: {wins}  Losses: {losses}  "
        f"(Win rate: {win_rate:.0f}%)\n"
    )
    if circuit_breaker_tripped:
        desc += "**🚨 Circuit breaker tripped today**\n"
    desc += "\n**Trades:**\n" + "\n".join(lines)
    send("📅 DAY SUMMARY", desc, GREEN if net >= 0 else RED)
