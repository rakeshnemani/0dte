"""src/commands.py — Thesis-GEX command rail (TODO #44).

Pure command-file model for the human-in-the-loop **`thesis`** strategy. An approved thesis is
dropped into ``data/commands/<id>.json`` as a JSON command; the bot (``src/bot.py``) scans this
directory each loop, watches each command's trigger, and executes / closes a single-leg SPX
option under the ``thesis:SPX`` slot.

**No IBKR here.** This module only reads / parses / validates command files and evaluates
trigger conditions against a spot price, so it is fully unit-testable (see
``scripts/test_thesis_commands.py``). All order placement, guards and P&L stay in ``bot.py``.

Boundary (see CLAUDE.md / TODO #44): Claude is the *analyst + translator*, the bot is the
*executor*, the user *authorises the arm*. A command file is a concrete, mechanical instruction —
the bot only executes deterministic triggers, it makes no discretionary call.

Command schema — one JSON object per file::

    {
      "id":     "arm-call-7710",           # required, unique — dedupe + reference key
      "cmd":    "arm" | "close" | "close_if" | "cancel",
      "symbol": "SPX",                     # optional (default = the bot's first symbol)
      "note":   "7700 pivot long ...",     # optional human/thesis text (Discord + audit)

      # cmd == "arm":
      "side":    "CALL" | "PUT",           # required
      "trigger": {"op": ">=", "level": 7710, "confirm_bars": 1},  # optional; omit/null = fire now
      "expires_at": "2026-08-20T16:00:00", # optional ISO ET; drop the arm if untriggered by then

      # cmd == "close_if":
      "target":  "thesis:SPX",             # optional (default thesis:<symbol>)
      "when":    {"op": "<=", "level": 7703},   # required spot condition
      "expires_at": "...",                 # optional

      # cmd == "cancel":
      "cancel_id": "arm-call-7710"         # required — id of the pending arm/close_if to drop
    }
"""
import datetime
import glob
import json
import operator
import os
import shutil

VALID_CMDS = ("arm", "close", "close_if", "cancel")
_OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt}


# ── Load / scan ──────────────────────────────────────────────────────────────

def scan(command_dir: str) -> list:
    """Parse every ``*.json`` command sitting directly in ``command_dir`` (never recurses
    into ``processed/``). Each result carries ``_path``; a malformed file comes back with
    ``_error`` set (and an ``id`` derived from the filename) so the caller can reject it
    rather than crash."""
    out = []
    if not command_dir or not os.path.isdir(command_dir):
        return out
    for path in sorted(glob.glob(os.path.join(command_dir, "*.json"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path) as f:
                cmd = json.load(f)
            if not isinstance(cmd, dict):
                raise ValueError("top-level JSON must be an object")
            cmd.setdefault("id", stem)
            cmd["_path"] = path
            out.append(cmd)
        except Exception as e:                       # malformed JSON / unreadable
            out.append({"id": stem, "_path": path, "_error": f"unparseable: {e}"})
    return out


def validate(cmd: dict):
    """Return ``(ok, error_message)``. Only structural validation — no market checks."""
    if cmd.get("_error"):
        return False, cmd["_error"]
    if not cmd.get("id"):
        return False, "missing 'id'"
    kind = cmd.get("cmd")
    if kind not in VALID_CMDS:
        return False, f"'cmd' must be one of {VALID_CMDS}, got {kind!r}"

    if kind == "arm":
        if cmd.get("side") not in ("CALL", "PUT"):
            return False, "arm needs 'side' = CALL or PUT"
        trig = cmd.get("trigger")
        if trig is not None:
            ok, err = _validate_condition(trig, "trigger")
            if not ok:
                return False, err
            cb = trig.get("confirm_bars", 1)
            if not isinstance(cb, int) or cb < 1:
                return False, "trigger.confirm_bars must be an int >= 1"
    elif kind == "close_if":
        ok, err = _validate_condition(cmd.get("when"), "when")
        if not ok:
            return False, err
    elif kind == "cancel":
        if not cmd.get("cancel_id"):
            return False, "cancel needs 'cancel_id'"

    if cmd.get("expires_at") is not None and _parse_dt(cmd["expires_at"]) is None:
        return False, f"unparseable 'expires_at': {cmd['expires_at']!r}"
    return True, ""


def _validate_condition(cond, name: str):
    if not isinstance(cond, dict):
        return False, f"'{name}' must be an object {{op, level}}"
    if cond.get("op") not in _OPS:
        return False, f"'{name}.op' must be one of {list(_OPS)}"
    if not isinstance(cond.get("level"), (int, float)):
        return False, f"'{name}.level' must be a number"
    return True, ""


# ── Trigger evaluation (pure) ────────────────────────────────────────────────

def arm_should_fire(arm: dict, spot: float, recent_closes: list) -> bool:
    """True when an arm's price trigger is met. No trigger → fire immediately. A
    ``confirm_bars`` of N requires the last N 1-minute closes to ALL satisfy the condition
    (so a one-tick wick past the level doesn't fire it)."""
    trig = arm.get("trigger")
    if not trig:
        return True                                  # immediate arm ("buy now")
    op = _OPS[trig["op"]]
    level = trig["level"]
    confirm = trig.get("confirm_bars", 1)
    closes = list(recent_closes or [])
    if len(closes) < confirm:
        return False
    return all(op(c, level) for c in closes[-confirm:])


def closer_should_fire(closer: dict, spot: float) -> bool:
    """True when a ``close_if`` spot condition is met right now (evaluated on the latest spot)."""
    when = closer["when"]
    return _OPS[when["op"]](spot, when["level"])


def is_expired(cmd: dict, now_et: datetime.datetime) -> bool:
    """True when ``expires_at`` has passed. Missing/unparseable → never expires."""
    exp = _parse_dt(cmd.get("expires_at"))
    if exp is None:
        return False
    now = now_et
    if exp.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    elif exp.tzinfo is not None and now.tzinfo is None:
        exp = exp.replace(tzinfo=None)
    return now >= exp


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


# ── Lifecycle ────────────────────────────────────────────────────────────────

def mark_processed(cmd: dict, command_dir: str, status: str) -> str:
    """Move a command file into ``command_dir/processed/`` (creating it), renaming it
    ``<status>-<id>-<HHMMSS>.json`` so a bot restart never re-runs it and there's an audit
    trail of what happened. Returns the new path (or '' if there was nothing to move)."""
    path = cmd.get("_path")
    if not path or not os.path.isfile(path):
        return ""
    done = os.path.join(command_dir, "processed")
    os.makedirs(done, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%H%M%S")
    dest = os.path.join(done, f"{status}-{cmd.get('id', 'cmd')}-{stamp}.json")
    try:
        shutil.move(path, dest)
        return dest
    except Exception:
        return ""


def describe(cmd: dict) -> str:
    """One-line human summary for logs / Discord."""
    kind = cmd.get("cmd")
    sym = cmd.get("symbol", "SPX")
    if kind == "arm":
        trig = cmd.get("trigger")
        when = "now" if not trig else f"{sym} {trig['op']} {trig['level']}"
        cb = f" x{trig['confirm_bars']}" if trig and trig.get("confirm_bars", 1) > 1 else ""
        return f"ARM {cmd.get('side')} when {when}{cb}"
    if kind == "close_if":
        w = cmd.get("when", {})
        return f"CLOSE {cmd.get('target', f'thesis:{sym}')} if {sym} {w.get('op')} {w.get('level')}"
    if kind == "close":
        return f"CLOSE {cmd.get('target', f'thesis:{sym}')} now"
    if kind == "cancel":
        return f"CANCEL {cmd.get('cancel_id')}"
    return kind or "?"
