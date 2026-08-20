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
| `close` | Close the `thesis:SPX` position now (if ACTIVE). |
| `close_if` | Close `thesis:SPX` when a spot condition is met (a conditional stop/exit). |
| `cancel` | Drop a still-pending `arm`/`close_if` by its id. |

Pending `arm`/`close_if` files stay here until they fire/expire/cancel (so a bot restart resumes
them); `close`/`cancel` run once and move immediately.

## Exits
A thesis trade uses the **same convex-tail exits as GEX**: trailing stop (arms at +50% peak, exits
giving back 20% of peak), a −80% catastrophe backstop, and the EOD flatten — **plus** any
`close`/`close_if` you send. It will never be held past the close.

## Examples

**Arm a CALL on an OR-break above 7710, 1-min confirmation (tomorrow's bullish case):**
```json
{
  "id": "arm-call-7710",
  "cmd": "arm",
  "side": "CALL",
  "note": "Bullish: break >7710 → runway to gflip 7731",
  "trigger": { "op": ">=", "level": 7710, "confirm_bars": 1 },
  "expires_at": "2026-08-20T13:00:00"
}
```

**Arm a PUT only on a decisive break-and-hold below 7700 (2-bar confirm):**
```json
{
  "id": "arm-put-7700",
  "cmd": "arm",
  "side": "PUT",
  "note": "Bearish: only if 7700 truly breaks (it has held twice)",
  "trigger": { "op": "<=", "level": 7700, "confirm_bars": 2 }
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
- `trigger` / `when` — `{ "op": ">="|">"|"<="|"<", "level": <number>, "confirm_bars": <int≥1> }`.
  `confirm_bars` (arm only) requires the last N 1-min closes to ALL satisfy the condition.
- `expires_at` (optional) — ISO ET (`YYYY-MM-DDTHH:MM:SS`); an untriggered arm/close_if is dropped after this.
