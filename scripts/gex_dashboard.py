"""GEX dashboard — turn a day's chain CSV into a visual thesis-forming page.

Reads the LAST snapshot of data/gex/chain_<date>.csv and renders a self-contained HTML file:
a horizontal **net-GEX-by-strike** bar chart (the canonical dealer-gamma profile) with spot +
Gflip marked, a metrics header, and the support/resistance ladders — so the heavy nodes, the
walls, and where spot sits relative to the flip are readable at a glance instead of by scanning
tables. Reuses src/gex.py so the numbers match the bot.

USAGE
    python scripts/gex_dashboard.py [YYYY-MM-DD]      # default: latest chain CSV
Output: data/gex/dashboard_<date>.html  (open in any browser; no dependencies)
"""
import csv
import glob
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import gex  # noqa: E402

GEX_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "gex")
NEG = "#e05252"      # put support (negative GEX)
POS = "#3fb950"      # call resistance (positive GEX)


def _load_last_snapshot(date):
    path = os.path.join(GEX_DIR, f"chain_{date}.csv")
    if not os.path.isfile(path):
        return None, None, None, None
    rows = list(csv.DictReader(open(path)))
    if not rows:
        return None, None, None, None
    last_ts = rows[-1]["timestamp"]
    snap = [r for r in rows if r["timestamp"] == last_ts]
    spot = float(snap[0]["spot"])
    chain = [{"strike": float(r["strike"]), "oi_call": float(r["oi_call"]),
              "oi_put": float(r["oi_put"]), "iv": float(r["iv"]), "T": float(r["T"])}
             for r in snap]
    return spot, chain, last_ts, path


def _svg_profile(spot, gflip, by_strike):
    """Horizontal net-GEX-by-strike bar chart (SVG). Highest strike at top, zero line at cx.
    Window is asymmetric so it always spans the support below AND the flip/resistance above."""
    lo = spot - 60
    hi = max(spot + 60, (gflip + 10) if gflip else spot + 60)
    strikes = sorted((k for k in by_strike if lo <= k <= hi), reverse=True)
    if not strikes:
        return "<p>no strikes in window</p>"
    maxabs = max(abs(by_strike[k]) for k in strikes) or 1.0
    row_h, top, cx, half = 22, 20, 430, 300      # geometry (cx = zero line; half = max bar length)
    lblx, width = 60, 860                          # strike-label column (fixed, far left)
    height = top + row_h * len(strikes) + 20
    y_of = {k: top + i * row_h + row_h / 2 for i, k in enumerate(strikes)}

    def y_interp(price):                          # y of an arbitrary price level
        khi, klo = strikes[0], strikes[-1]
        if price >= khi:
            return y_of[khi]
        if price <= klo:
            return y_of[klo]
        frac = (khi - price) / (khi - klo)
        return top + frac * (row_h * (len(strikes) - 1)) + row_h / 2

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px">']
    parts.append(f'<line x1="{cx}" y1="{top-6}" x2="{cx}" y2="{height-14}" stroke="#555" stroke-width="1"/>')
    for k in strikes:
        v = by_strike[k]
        y = y_of[k]
        blen = abs(v) / maxabs * half
        color = NEG if v < 0 else POS
        x = cx - blen if v < 0 else cx
        parts.append(f'<rect x="{x:.1f}" y="{y-row_h/2+3:.1f}" width="{blen:.1f}" height="{row_h-6}" '
                     f'fill="{color}" opacity="0.85" rx="2"/>')
        parts.append(f'<text x="{lblx}" y="{y+4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="#bbb">{k:.0f}</text>')
        lx = (cx - blen - 6) if v < 0 else (cx + blen + 6)
        anc = "end" if v < 0 else "start"
        parts.append(f'<text x="{lx:.1f}" y="{y+4:.1f}" text-anchor="{anc}" font-size="10" '
                     f'fill="#8b949e">{v/1e6:+,.0f}M</text>')
    # spot + gflip marker lines (labels anchored to the right edge so they never clip)
    ys = y_interp(spot)
    parts.append(f'<line x1="{lblx+10}" y1="{ys:.1f}" x2="{width-96}" y2="{ys:.1f}" stroke="#f0c040" '
                 f'stroke-width="1.5" stroke-dasharray="6 3"/>')
    parts.append(f'<text x="{width-8}" y="{ys+4:.1f}" text-anchor="end" font-size="11" '
                 f'font-weight="600" fill="#f0c040">SPOT {spot:.0f}</text>')
    if gflip and strikes[-1] <= gflip <= strikes[0]:
        yg = y_interp(gflip)
        parts.append(f'<line x1="{lblx+10}" y1="{yg:.1f}" x2="{width-96}" y2="{yg:.1f}" stroke="#58a6ff" '
                     f'stroke-width="1.5" stroke-dasharray="2 3"/>')
        parts.append(f'<text x="{width-8}" y="{yg+4:.1f}" text-anchor="end" font-size="11" '
                     f'font-weight="600" fill="#58a6ff">GFLIP {gflip:.0f}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def _ladder_table(title, rows, color):
    trs = "".join(f"<tr><td>{k:.0f}</td><td style='color:{color}'>{v/1e6:+,.0f}M</td></tr>"
                  for k, v in rows)
    return (f"<table><caption>{title}</caption>"
            f"<tr><th>Strike</th><th>Net GEX</th></tr>{trs}</table>")


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else None
    if not date:
        chains = sorted(glob.glob(os.path.join(GEX_DIR, "chain_*.csv")))
        if not chains:
            print("No chain CSVs found in data/gex/."); return 1
        date = os.path.basename(chains[-1])[len("chain_"):-len(".csv")]

    spot, chain, ts, path = _load_last_snapshot(date)
    if not chain:
        print(f"No usable chain snapshot for {date}."); return 1

    gflip = gex.gamma_flip(chain, spot)
    regime = gex.gex_regime(spot, gflip)
    net_total = gex.net_gex(spot, chain) / 1e6
    net_0dte = gex.net_gex_0dte(spot, chain) / 1e6
    calls, puts = gex.gex_ladders(spot, chain, n=6)
    by_strike = gex.gex_by_strike(spot, chain)
    zones = gex.concentration_zones(chain, n=1)
    cw = zones["call_walls"][0][0] if zones["call_walls"] else 0
    pw = zones["put_walls"][0][0] if zones["put_walls"] else 0
    dist = (spot - gflip) / gflip * 100 if gflip else 0

    reg_color = NEG if regime == "negative" else POS
    metrics = [
        ("Spot", f"{spot:.2f}", "#f0c040"),
        ("Gflip", f"{gflip:.2f}", "#58a6ff"),
        ("Distance", f"{spot-gflip:+.1f} ({dist:+.2f}%)", reg_color),
        ("Regime", regime.upper(), reg_color),
        ("Net GEX (total)", f"{net_total:,.0f}M", "#ddd"),
        ("Net GEX (0DTE)", f"{net_0dte:,.0f}M", "#ddd"),
        ("Call Wall (OI)", f"{cw:.0f}", POS),
        ("Put Wall (OI)", f"{pw:.0f}", NEG),
    ]
    cards = "".join(
        f"<div class='card'><div class='lbl'>{lbl}</div>"
        f"<div class='val' style='color:{c}'>{val}</div></div>" for lbl, val, c in metrics)

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>GEX Dashboard {date}</title>
<style>
  body{{background:#0d1117;color:#e6edf3;font:14px -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}}
  h1{{font-size:18px;margin:0 0 2px}} .sub{{color:#8b949e;font-size:12px;margin-bottom:16px}}
  .cards{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:20px}}
  .card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 14px;min-width:120px}}
  .lbl{{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
  .val{{font-size:18px;font-weight:600;margin-top:3px}}
  .grid{{display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start}}
  .chart{{flex:1;min-width:520px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px}}
  table{{border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:8px}}
  caption{{text-align:left;font-weight:600;padding:8px 10px;color:#c9d1d9}}
  th,td{{padding:5px 14px;text-align:right;font-size:13px;border-top:1px solid #21262d}}
  th{{color:#8b949e;font-weight:500}}
  .note{{color:#8b949e;font-size:12px;margin-top:14px;max-width:820px;line-height:1.5}}
</style></head><body>
<h1>GEX Dashboard — {date}</h1>
<div class="sub">snapshot {ts} · source {os.path.basename(path)} · net GEX = our-convention $M (±5% / 3-expiry window)</div>
<div class="cards">{cards}</div>
<div class="grid">
  <div class="chart">
    <div style="color:#8b949e;font-size:12px;margin-bottom:8px">
      Net GEX by strike &nbsp;·&nbsp; <span style="color:{NEG}">▉ put support (−)</span>
      &nbsp; <span style="color:{POS}">▉ call resistance (+)</span></div>
    {_svg_profile(spot, gflip, by_strike)}
  </div>
  <div>
    {_ladder_table("Support ladder (heaviest −GEX)", puts, NEG)}
    <div style="height:14px"></div>
    {_ladder_table("Resistance ladder (heaviest +GEX)", calls, POS)}
  </div>
</div>
<div class="note"><b>Read it:</b> the longest red bars are the heaviest put-support nodes (where dealers
defend / price magnets); the longest green bars are call resistance. Spot (yellow) vs Gflip (blue) sets the
regime. <b>Runway</b> = enter a PUT <i>below</i> the support cluster or a CALL <i>above</i> the resistance
shelf (room to run); <b>IntoWall</b> = entering into a heavy node. Wait out the noise zone (the node cluster
± a small buffer) and take the break.</div>
</body></html>"""

    out = os.path.join(GEX_DIR, f"dashboard_{date}.html")
    with open(out, "w") as f:
        f.write(html)
    print(f"✅ wrote {out}")
    print(f"   spot {spot:.2f} · Gflip {gflip:.2f} ({dist:+.2f}%, {regime}) · "
          f"net {net_total:,.0f}M · heaviest support {puts[0][0]:.0f} ({puts[0][1]/1e6:,.0f}M)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
