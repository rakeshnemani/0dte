# Thesis-GEX — human-in-the-loop trading (TODO #44)

A third strategy slot, `thesis:SPX`, that runs **alongside** the mechanical `trend` + `gex`
scanners (they are untouched). It exists to close the gap 08-18/08-19 exposed: the bot's
mechanical read was wrong while the human read was right, and there was no rail to act on the
human call.

> **The story so far:** two straight days the mechanical GEX strategy bought a PUT *below* the
> 7720 put-support wall (`Setup_Tag=IntoWall`), price bounced off support and reverted up, and
> the trade rode to the −80% catastrophe stop (−$800, −$1,115). On both days the *human* read
> (bullish / wait-for-a-real-break) would have won. This rail lets that judgment reach the bot.

## The flow

```
  you (mobile)          Claude (analyst)              the bot (executor)
  ──────────────        ────────────────────          ───────────────────────
  send a concrete  ───▶ vet vs live GEX          ───▶ watch the trigger every loop
  thesis               (go / no-go / modify)          fire ONE ATM CALL/PUT under
                       write an `arm` command          thesis:SPX (all guards apply)
                       file you authorise             manage exits; you can `close` anytime
```

**Boundary (unchanged from the design):** Claude is the **analyst + translator**, the bot is the
**executor**, **you authorise the arm**. Claude never discretionarily fires a live order — it turns
an approved thesis into a *deterministic* armed instruction (level + confirmation + side + size +
exit) and the bot pulls the trigger mechanically, exactly like `trend`/`gex` do. A command file is
data, not a discretionary act.

## How it works (part c — the command rail, **built**)

The bot polls `data/commands/` every loop. Drop one JSON object per file; the bot acts on it and
moves it to `data/commands/processed/` so it never runs twice. **Full schema + copy-paste examples:
[`data/commands/README.md`](../data/commands/README.md).**

| `cmd` | Effect |
|-------|--------|
| `arm` | Watch a trigger; when met, buy ONE ATM CALL/PUT under `thesis:SPX`. Trigger is either a **price level** (`op`/`level`) or an **`or_breakout`** — a break of the 15-min opening-range high (CALL) / low (PUT), computed the same way the mechanical GEX entry does, only after the OR completes, with an optional `min_level`/`max_level` noise floor. No trigger = fire now. `expires_at` drops it if untriggered. |
| `close` | Close `thesis:SPX` now (if ACTIVE). |
| `close_if` | Close `thesis:SPX` when a spot condition (`when`) is met — a conditional stop/exit. |
| `cancel` | Drop a still-pending `arm`/`close_if` by id. |

- **Files are the source of truth.** Pending `arm`/`close_if` are rebuilt from the files each
  loop, so a **bot restart resumes them**; one-shot `close`/`cancel` run once and move immediately.
- **`confirm_bars`** requires the last N 1-minute closes to ALL satisfy the condition, so a
  one-tick wick past a level doesn't fire the arm.
- **Every account guard still applies** — the arm fires through the normal entry path
  (`execute_trade`): anti-cascade untracked-position guard, circuit breaker, daily-loss limit,
  trade cap, tick-snapped limit. A sticky guard block consumes the arm (fire-once — no retry loop);
  you re-arm if needed.
- **GEX context is frozen** at fire time exactly like a mechanical GEX trade (Gflip, distance,
  net GEX, the top-3 ladders, and the `Setup_Tag` bucket) — so thesis trades are directly
  comparable in the audit (was the human right about runway?).

## Exits

A thesis trade uses the **same convex-tail exits as GEX** (routed through `_gex_exit_check`):
trailing stop (arms at +35% peak, exits on giving back 20% of peak) + a −80% catastrophe backstop
+ the EOD flatten — **plus** any `close`/`close_if` you send. It is never held past the close.
(Default chosen for the connectivity case: an unattended winner is still protected; you can always
close earlier by hand.)

## Config

```
THESIS_ENABLED=true                 # master switch (default on)
THESIS_COMMAND_DIR=data/commands    # relative → resolved to repo root
```

## Code map

| File | Role |
|------|------|
| `src/commands.py` | **Pure** command model: load / validate / trigger-evaluation / expiry / mark-processed. No IBKR — fully unit-tested. |
| `src/bot.py` | Orchestration: `_process_thesis_commands`, `_watch_thesis_triggers`, `_fire_thesis_arm`, `_thesis_close_now`, `_thesis_cancel`; `_freeze_gex_context` (shared with GEX); exit routing. |
| `src/notifier.py` | `notify_thesis_action` — armed / fired / close / cancelled / expired / blocked / rejected. |
| `src/config.py` | `THESIS_ENABLED`, `THESIS_COMMAND_DIR`. |
| `data/commands/` | The rail: live command files (git-ignored) + `README.md` schema + `processed/` archive. |
| `scripts/test_thesis_commands.py` | 47 checks: pure validation/triggers + integration (arm fires on trigger, close_if, cancel, expiry, malformed rejection). |

## Using it today (before Discord)

The rail works **through this chat right now** — no Discord needed:
1. You send me a concrete thesis (like the 08-20 one).
2. I vet it against live GEX and, once you say "arm it," write the `arm` command file into
   `data/commands/`.
3. The running bot fires it when the trigger is met and manages the exit; you say "close" anytime.

## Roadmap

- **(c) Command rail — ✅ built + tested** (this doc).
- **(b) The analyst — ✅ works** (see the 08-20 thesis evaluation in the chat / RETROSPECTIVE).
- **(a) Discord channel — ⏳ next.** A dedicated channel routed to a 0dte Claude session (separate
  from the *ImpliedMoveBasedOptionSelector* pairing) so theses can come from mobile. This is a
  convenience layer on top of a working engine, not a prerequisite.

Related: the separate, evidence-gated **IntoWall guard** for the *mechanical* gex strategy is
TODO #43 (n=2, not built) — a different category from this human-thesis rail.

## Observations under watch

- **Confirmation uses the LIVE (in-progress) bar, not only completed closes (noted 2026-08-21, n=1).**
  `_watch_thesis_triggers` reads `df['close']`, whose last element is the *current, still-forming* 1-min
  bar (IBKR `reqHistoricalData(endDateTime='')` returns the partial bar). So a `confirm_bars: 2` check is
  really *"last completed close + current live price,"* not *"two completed closes."* On 08-21 the CALL
  fired 11:01 on an in-progress bar showing ~7679; that bar then **closed back below** (11:01=7676.55,
  11:02=7676.35) before the real break at 11:03–11:04 → the entry ate a **−39.88% MAE** dip it survived
  only because there is no fixed stop. A stricter *"N completed closes"* rule would have entered ~11:04,
  after the pullback, skipping the dip (at a slightly higher premium). **Not a change yet — n=1.** To A/B
  this properly we'd need to persist 1-min bars or log each trigger evaluation (we don't today; the 08-21
  reconstruction only worked because the Gateway happened to be up). See TODO #45.

  **P&L of the two on 08-21 (from the 1-min reconstruction):** live-bar entered 8.40 @ 11:01 → +$590;
  strict would have entered ~8.5–9.2 @ ~11:04–11:05 (est.; 1-min option marks aren't logged) → ~+$530–590.
  So **P&L was roughly a wash, live-bar a hair ahead** — the cheaper early entry slightly *beat* the
  calmer one, because the trailing stop is percentage-of-entry (a higher entry makes the same 16.00 peak a
  smaller % gain, so it trails out lower). **It's a variance trade-off, not a free lunch:**
  - *live-bar (current):* cheaper entries, bigger MAE, and it will sometimes enter fakeouts **that don't
    recover** — on a day the −40% runs to the −80% catastrophe, this version is *much worse*.
  - *completed-bar (strict):* pricier/calmer entries, misses some cheap fills, but **dodges the
    fakeout-losers**.
  08-21 the fakeout recovered so live-bar won by a hair; on a day it doesn't, strict wins big. The decision
  hinges on the fakeout recover-rate, which is exactly what n=1 can't tell us — hence: watch, log 1-min bars, decide later.
