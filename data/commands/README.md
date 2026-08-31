# Thesis-GEX command rail — `data/commands/`

The bot polls this directory every loop (see `src/commands.py` + `bot._process_thesis_commands`).
Drop a **single JSON object per file** here (`<id>.json`) and the bot will act on it, then move it
to `processed/` (renamed `<status>-<id>-<HHMMSS>.json`) so it never runs twice.

This is the **`thesis` strategy** — a human-authorised trade that runs *alongside* the mechanical
`trend` + `gex` scanners (they are untouched). Trades land under the `thesis:SPX` slot. Boundary:
Claude is the analyst/translator, the bot is the executor, **you authorise the arm**.

## Commands

| `cmd` | What it does |
|-------|--------------|
| `arm` | Watch a price trigger; when met, buy ONE ATM `CALL`/`PUT` under `thesis:SPX`. No trigger = fire now. |
| `close` | Close a position now. `target` picks which: `thesis:SPX` (default), `gex:SPX`, `trend:SPX`, or **`"all"`** to flatten everything open. |
| `close_if` | Close a position (`target`, default `thesis:SPX`) when a spot condition (`when`) is met — a conditional stop/exit. |
| `cancel` | Drop a still-pending `arm`/`close_if` by its id. |

Pending `arm`/`close_if` files stay here until they fire/expire/cancel (so a bot restart resumes
them); `close`/`cancel` run once and move immediately.

## Exits
A thesis trade uses the **same convex-tail exits as GEX**: trailing stop (arms at +50% peak, exits
giving back 20% of peak), a −80% catastrophe backstop, and the EOD flatten — **plus** any
`close`/`close_if` you send. It will never be held past the close.

## Examples

**Arm a CALL on a break of the 15-min opening-range high (the faithful "OR breakout"):**
```json
{
  "id": "arm-call-or",
  "cmd": "arm",
  "side": "CALL",
  "note": "Bullish: 1m break above the 15-min OR high → runway to gflip 7731",
  "trigger": { "type": "or_breakout", "or_minutes": 15, "min_level": 7715, "confirm_bars": 1 },
  "expires_at": "2026-08-20T15:55:00"
}
```
The bot waits until the 15-min opening range completes (9:45), computes the OR high the same way
the mechanical GEX entry does, then fires when a 1-min close breaks it. `min_level` clamps the
trigger up so it also waits out a noise band (fires on `close ≥ max(OR_high, 7715)`).

**Arm a CALL on a fixed price level instead (no OR):**
```json
{
  "id": "arm-call-7710",
  "cmd": "arm", "side": "CALL",
  "trigger": { "op": ">=", "level": 7710, "confirm_bars": 1 }
}
```

**Arm a PUT only on a decisive break of the OR low, floored below 7700 (2-bar confirm):**
```json
{
  "id": "arm-put-or",
  "cmd": "arm",
  "side": "PUT",
  "note": "Bearish: only if the OR low AND 7700 truly break (7700 has held twice)",
  "trigger": { "type": "or_breakout", "max_level": 7700, "confirm_bars": 2 }
}
```

**Buy a CALL right now (no trigger):**
```json
{ "id": "buy-now-call", "cmd": "arm", "side": "CALL", "note": "manual entry" }
```

**Close the thesis position now:**
```json
{ "id": "close-now", "cmd": "close", "note": "taking it off" }
```

**Close a *specific* slot (the mechanical gex trade, or trend):**
```json
{ "id": "close-gex", "cmd": "close", "target": "gex:SPX", "note": "manual gex exit" }
```

**Flatten EVERYTHING open (thesis + gex + trend):**
```json
{ "id": "flatten", "cmd": "close", "target": "all", "note": "panic flatten" }
```

**Conditional exit — close if SPX trades back to the 7703 pivot:**
```json
{ "id": "stop-7703", "cmd": "close_if", "when": { "op": "<=", "level": 7703 } }
```

**Cancel a pending arm:**
```json
{ "id": "cancel-1", "cmd": "cancel", "cancel_id": "arm-put-7700" }
```

## Fields
- `id` (required, unique) — dedupe + reference key. Also the default filename stem.
- `symbol` (optional) — defaults to the bot's first symbol (`SPX`).
- `note` (optional) — free text; shows in Discord + the audit `Reason`.
- `trigger` (arm) — one of:
  - **price** (default): `{ "op": ">="|">"|"<="|"<", "level": <number>, "confirm_bars": <int≥1> }`
  - **OR breakout**: `{ "type": "or_breakout", "or_minutes": 15, "confirm_bars": 1, "min_level": <num>, "max_level": <num> }`
    — fires when a close breaks the 15-min opening-range **high** (CALL) / **low** (PUT), only after
    the OR window completes. Optional `min_level` (CALL) / `max_level` (PUT) clamp the derived level
    to also wait out a noise band. `or_minutes` defaults to `GEX_OR_MINUTES` (15).
  - Omit `trigger` entirely to **fire immediately** ("buy now").
- `when` (close_if) — a price condition: `{ "op": "<="|..., "level": <number> }`.
- `confirm_bars` — requires the last N **completed** 1-min closes to ALL satisfy the condition (the bot drops
  the current, still-forming bar, so N=2 = two *closed* bars, not "one close + the live tick" — a wick through
  the level won't fire it; 2026-08-31).
- `expires_at` (optional) — ISO ET (`YYYY-MM-DDTHH:MM:SS`); an untriggered arm/close_if is dropped after this.
