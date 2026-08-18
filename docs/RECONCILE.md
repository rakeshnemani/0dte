# Reconciling the bot against IBKR — `scripts/reconcile_ibkr.py`

The bot's `audit.csv` is *what the bot thinks happened*. Your IBKR account is *what
actually happened*. When those disagree — an orphaned position, a fill that didn't
book, an expiry that settled worse than expected — you want to know. This tool joins
the two, **keyed on `permId`** (IBKR's permanent order id), and shows every mismatch.

It is **read-only** against IBKR: it places no orders and cancels nothing. The only
thing it can write is `audit.csv`, and only with the explicit `--write` flag (which
backs up first).

---

## When to use it

- **End-of-day truth check** — the single most useful habit. After the market settles,
  run it for the day; if the audit and the account agree, you're clean.
- **Investigating a ⚠️ alert** — a "position closed externally", "close failed", or
  "untracked position" alert. Run it to see what IBKR actually holds/booked.
- **Verifying a suspicious P&L** — the audit shows one number, your statement another.
- **Historical review** — reconcile any past day (needs a Flex Query, see below).

> ⚠️ **Run it AFTER settlement, not intraday.** 0DTE options mark unreliably near
> expiry. On 2026-07-09 a *pre-settlement* run showed −$124 and looked fine; the
> *post-settlement* run showed the true **−$906**. Only settled account `dailyPnL`
> is truth — wait until after ~4:15 PM ET (ideally evening) for a final number.

---

## Prerequisites

1. **IB Gateway running and logged in** (paper), API enabled, port 4002 — same as the
   bot. The tool connects on `clientId=9`, so it can run **while the bot is running**
   (the bot uses `clientId=1`).
2. Your venv active (`source .venv/bin/activate`).

---

## Usage

```bash
python scripts/reconcile_ibkr.py                  # today
python scripts/reconcile_ibkr.py 2026-07-09       # a specific recent day (~last 24h)
python scripts/reconcile_ibkr.py 2026-06-15       # older day — auto-uses a Flex Query if configured
python scripts/reconcile_ibkr.py 2026-07-09 --write   # also append orphan flags to audit.csv
```

- **No date** → today. **A date** (`YYYY-MM-DD`) → that day.
- Dates within ~24h use the **live API** (`reqExecutions` + account `dailyPnL`).
- Older dates need an **IBKR Flex Query** (the live API only keeps ~24h). See
  [Historical dates](#historical-dates-flex-query).
- **`--write`** appends a `RECONCILE` row to `audit.csv` for each orphan IBKR order
  (an order with no audit row), so the books capture it. It copies `audit.csv` →
  `audit.csv.bak` first. Off by default — the tool never mutates your books unasked.

---

## Reading the output

Real example — the 2026-07-09 run (abridged):

```
=== permId reconciliation for 2026-07-09 ===
IBKR orders: 30   |   audit rows with permId: 9

      permId      time  sym  side  price   realized    comm  audit
  1015042438  10:24:55  QQQ  BOT    0.33      +0.00   11.92  OK
  1015042440  10:27:13  QQQ  SLD    0.29     -82.14   18.22  OK
  1015042447  11:28:49  IWM  BOT    0.18     +49.23    0.54  <-- ORPHAN (not booked)
  ...
   280344937  21:44:37  SPY         0.00      +0.00    0.00  <-- ORPHAN (not booked)   # expiry settlement

--- summary ---
Matched IBKR orders: 9   |   ORPHAN IBKR orders (no audit row): 21
!!  Audit BUYs with no SELL today: SPYx1, IWMx1  (opened, never booked closed)
IBKR realized P&L: $-218.29   commissions: $100.98
Account dailyPnL (incl. expiry settlement): $-905.99  <-- true day total
Audit booked: gross $-189.00   fees $107.73   net $-296.73
```

**The per-order table** — one line per IBKR order, sorted by time:

| Column | Meaning |
|--------|---------|
| `permId` | IBKR's permanent order id — the join key. Big `1015…` numbers are bot orders; small numbers (e.g. `280…`) at end-of-day are **expiry/exercise settlements** |
| `side` | `BOT` (bought) / `SLD` (sold) the option |
| `price` | the option fill price |
| `realized` | IBKR realized P&L on that order, **net of commissions** (0 on opening orders) |
| `comm` | commissions on that order's fills |
| `audit` | **`OK`** = matched to an audit row by permId · **`ORPHAN`** = *no audit row exists* — a fill the bot didn't book |

**Two independent orphan detectors** — this is the point of the tool:
1. **`ORPHAN` rows** — IBKR did something the bot has no record of.
2. **`Audit BUYs with no SELL today`** — the bot opened a position and never booked a
   close (the classic orphaning signature — `SPYx1, IWMx1` above are exactly the two
   positions the reconciliation bug lost on 07-09).

**The reconciliation numbers** — read these top-down:
- `Account dailyPnL` — **the truth.** All-in, net of fees, includes expiry settlement.
  This is the number to trust and to record.
- `IBKR realized P&L` — sum of realized on *closed* orders (excludes expiry, so it can
  differ a lot from `dailyPnL` when positions expire — as above, −$218 vs −$906).
- `Audit booked net` — what the bot's books say. **If this ≠ `dailyPnL`, the gap is
  unbooked orphans and/or settlement** — investigate.

**Still-open positions** (if any) — legs the account holds *right now*. During market
hours these are live positions; after the close they're pending settlement.

---

## Common workflows

**Nightly truth check**
```bash
python scripts/reconcile_ibkr.py            # after settlement
```
If `Matched == IBKR orders`, no `ORPHAN`s, no unclosed BUYs, and `dailyPnL` ≈ `Audit
booked net` → clean day. Otherwise, dig in.

**Book orphans into the audit** (after confirming they're real)
```bash
python scripts/reconcile_ibkr.py 2026-07-09 --write
```

**Backfill missing permIds first** (for recent audit rows written before permId
logging, so the join is exact)
```bash
python scripts/backfill_permid.py           # fills PermId on recent audit rows
python scripts/reconcile_ibkr.py 2026-07-09 # now joins cleanly
```

---

## Historical dates (Flex Query)

The live API only keeps ~24 hours of executions. For older dates, IBKR's **Flex Web
Service** serves reports up to ~1 year. One-time setup:

1. IBKR **Client Portal → Settings → Flex Queries** → create a **Trade Confirmation**
   Flex Query. Note its **Query ID**.
2. **Reporting → Flex Web Service** → enable it and generate a **token**.
3. Put both in `.env`:
   ```env
   IBKR_FLEX_TOKEN=your_token
   IBKR_FLEX_QUERY_ID=your_query_id
   ```

Then `python scripts/reconcile_ibkr.py 2026-06-15` automatically uses the Flex path for
out-of-window dates. (Flex reports don't include account `dailyPnL`, so historical runs
sum per-trade realized P&L instead.)

---

## Gotchas

- **Run after settlement** — see the warning above. Intraday 0DTE marks lie.
- **`clientId=9`** — safe to run alongside the bot (`clientId=1`). If you see a client-id
  conflict, another tool is already using 9; change it at the top of the script.
- **Ragged audit rows** — older rows predate newer columns (`Commission`, `PermId`);
  the tool handles the missing cells. Only recent rows can carry a permId.
- **`RECONCILE` rows** — `--write` and manual corrections add `Action=RECONCILE` rows.
  They're annotations, not trades; the dashboard and stats treat them accordingly.

---

## Related

- `scripts/backfill_permid.py` — retro-fill `PermId` on recent audit rows (24h window).
- `audit.csv` — the bot's financial ledger ([README](../README.md#audit-log)).
- `logs/bot.log` — the operational log (what the bot *did*), separate from financials.
