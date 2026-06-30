# 0DTE Bot — Improvement Backlog

Items to validate or implement once the core strategy has enough paper-trade history.

---

## Strategy Improvements

1. **ADX slope check (rising vs. flat)** — The current filter only checks `ADX > 25` at the moment of entry. A market can have ADX = 28 because it was trending earlier but is now going sideways. A *rising* ADX (e.g. 20 → 28 over the last 10 bars) is a much stronger signal than a flat or declining ADX at the same level. Add a check that ADX has been increasing over the last N bars before allowing entry.

---
