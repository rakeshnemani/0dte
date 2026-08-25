# 0DTE Paper Trading Bot

A Python algorithmic bot that paper-trades **0DTE single-leg options** (SPX) on Interactive Brokers
(IBKR), running **three position slots** together: two mechanical strategies — **Trend** (Supertrend +
PSAR + Kaufman-chop) and **GEX** (dealer gamma-flip momentum) — plus a **human-thesis rail** (`thesis:SPX`,
added 2026-08-19): the user forms a daily GEX thesis, Claude vets it, and an approved thesis is armed as a
command file the bot executes. It buys ONE ATM (~50Δ) CALL/PUT per signal — a single leg, one contract.

> **Goal:** reach consistent, *fee-adjusted* profitability on paper, then go live. Read
> [CLAUDE.md](CLAUDE.md) (start with its **"📌 Current state & handoff"** block) for the full orientation,
> [docs/PLAYBOOKS.md](docs/PLAYBOOKS.md) for entry/exit rules, and [docs/THESIS_GEX.md](docs/THESIS_GEX.md)
> for the human-thesis command rail.

## Project structure

```text
0dte/
├── .env                 # Configuration (git-ignored)
├── README.md
├── requirements.txt
├── CLAUDE.md            # Canonical orientation — read first
├── src/
│   ├── main.py         # Entry point: asyncio loop + logging, then TradingBot().run()
│   ├── config.py       # All .env config
│   ├── bot.py          # TradingBot — state + orchestration + main loop
│   ├── broker.py       # IBKRBroker — all IBKR calls
│   ├── strategy.py     # Pure signal/exit functions (indicators, trend + gex signals)
│   ├── commands.py     # Pure thesis-rail command model (arm/close/close_if/cancel)
│   ├── gex.py          # Dealer-gamma math (Gflip, walls, ladders, expected_move)
│   ├── notifier.py     # Discord alerts
│   ├── audit.py        # audit.csv writer
│   └── market_time.py  # ET market-hours helpers
├── data/commands/      # thesis-rail command files (dropped by Claude, watched by the bot)
├── data/gex/           # saved GEX chains + regime CSVs + dashboards/ (the visual boards)
├── scripts/            # reconcile, gex_snapshot, gex_dashboard, backfill, backtest, tests
└── docs/               # THESIS_GEX, HOW_IT_WORKS, PLAYBOOKS, GEX_NOTES, RETROSPECTIVE, GO_LIVE, …
```

## Prerequisites

### 1. IB Gateway (or TWS) — paper mode
Install IB Gateway, log into the **paper** account, and enable the API (Configure → Settings → API →
Enable ActiveX and Socket Clients; socket port **4002** for Gateway paper, 7497 for TWS paper).

### 2. Options data + permissions
The paper account needs US index-options permissions and a real-time market-data subscription (paper
inherits the live account's). SPX is cash-settled and European-style, so there is **no assignment risk**.

### 3. Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure `.env`
```bash
# IBKR connection
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
IBKR_CLIENT_ID=1
IBKR_MARKET_DATA_TYPE=1        # 1=live/real-time

# Strategy
SYMBOLS=SPX
STRATEGY=trend,gex

# Trend
TREND_WINDOWS=09:30-15:55
TREND_KAUF_MAX=50
TREND_SKIP_LOWIV=0.082

# GEX exits (let the convex tail ride)
GEX_TRAIL_TRIGGER=0.35         # arm the trailing stop once peaked +35% (lowered from 0.50 on 08-24)
GEX_TRAIL_GIVEBACK=0.20        # exit if it gives back 20% of the peak
GEX_CATASTROPHE_STOP=0.80      # wide backstop for a trade that never peaks

# Shared guards
MIN_OPTION_COST=0.30           # skip if the option mid is below this (fee floor)
HARD_STOP_LOSS_PCT=0.50        # trend hard stop
MAX_TRADES_PER_DAY=12
MAX_DAILY_LOSS=800
EOD_FLATTEN_TIME=15:55

DISCORD_WEBHOOK_URL=...
```

## Running the bot

```bash
python src/main.py
```
The bot runs during market hours (09:30–16:00 ET) and sleeps otherwise. **Restart it to apply any code
or `.env` change** — state is in-memory. Don't restart with a position open (see the adoption note in
[CLAUDE.md](CLAUDE.md)).

## Strategy

Two **single-leg directional** strategies run together, each holding its own SPX position (keyed
`strategy:symbol`). Both buy ONE ATM (~50Δ) option, 1 contract, **no take-profit** (the convex tail is
the edge). Full details in [docs/PLAYBOOKS.md](docs/PLAYBOOKS.md).

| Strategy | Entry | Exits |
|---|---|---|
| **Trend** | Supertrend(7,3) flip + PSAR agree + Kaufman-chop ≤ 50, inside `TREND_WINDOWS`, + vol gate | −50% stop · Supertrend reversal · EOD flatten |
| **GEX** | negative-gamma / wall breakout + 15-min opening-range breakout + momentum, inside `GEX_WINDOWS`, + vol gate (Gflip computed live from the IBKR chain) | trailing (arm +35%, give back 20% of peak) · −80% catastrophe backstop · EOD flatten |
| **Thesis** | human-in-the-loop: an approved daily GEX thesis armed as a `data/commands/*.json` command (price / OR-breakout trigger) — see [docs/THESIS_GEX.md](docs/THESIS_GEX.md) | same as GEX (trailing +35% / −80% / EOD) + any `close`/`close_if` you send |

**Shared guards:** cooldown (30 min), circuit breaker (5 consecutive losses), daily loss limit,
12 trades/day, anti-cascade (never stack on an untracked position).

## Discord alerts

| Alert | Colour | When |
|---|---|---|
| ⏳ ORDER SUBMITTED | 🟠 | The single-leg BUY is sent to IBKR (fires even if later rejected) |
| 🟢 NEW 0DTE ENTRY | 🟢 | The entry fills |
| 🔵 CLOSED 0DTE POSITION | 🔵/🔴 | A position is closed (with P&L) |
| ⏸️ SETUP SKIPPED | ⚪ | A setup formed but a filter blocked the trade (process transparency) |
| 🔌 DATA FEED DOWN / ✅ RESTORED | 🔴/🟢 | IBKR data-farm drop/restore |
| ⚠️ / ⛔ risk alerts | 🔴 | Circuit breaker, daily loss limit, untracked position, close-failed |

## Record-keeping

- **`audit.csv`** — one row per fill; `PermId` joins each row to IBKR. Reconcile with
  `scripts/reconcile_ibkr.py` **after settlement**.
- **`logs/bot.log`** — operational log (daily rotation, ET).
- **`dashboard.xlsx`** — regenerated after each trading day.
