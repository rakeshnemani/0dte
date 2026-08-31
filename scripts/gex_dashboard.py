"""GEX dashboard — turn a day's chain CSV into a visual thesis-forming page.

Reads the LAST snapshot of data/gex/chain_<date>.csv and renders a self-contained HTML file:
a horizontal **net-GEX-by-strike** bar chart (the canonical dealer-gamma profile) with spot +
Gflip marked, a metrics header (walls are **gamma-weighted**, so they match the bars), the
support/resistance ladders, and an **open-interest-by-strike line chart** (call vs put OI — the
raw contract magnets like round-number 7700 that pile up huge OI but net to ~0 gamma, so they're
invisible in the net-GEX chart). Reuses src/gex.py so the numbers match the bot.

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


def _latest_regime(date):
    """Most recent (spot, timestamp) from regime_<date>.csv — logged every ~5 min, so it's much
    fresher than the 30-min-throttled chain. Used to move the spot marker in ~real time."""
    p = os.path.join(GEX_DIR, f"regime_{date}.csv")
    if not os.path.isfile(p):
        return None
    rows = [r for r in csv.DictReader(open(p)) if r.get("spot")]
    if not rows:
        return None
    try:
        return float(rows[-1]["spot"]), rows[-1]["timestamp"]
    except (ValueError, KeyError):
        return None


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
                     f'font-size="13" fill="#bbb">{k:.0f}</text>')
        lx = (cx - blen - 6) if v < 0 else (cx + blen + 6)
        anc = "end" if v < 0 else "start"
        parts.append(f'<text x="{lx:.1f}" y="{y+4:.1f}" text-anchor="{anc}" font-size="10" '
                     f'fill="#8b949e">{v/1e6:+,.0f}M</text>')
    # spot + gflip marker lines (labels anchored to the right edge so they never clip)
    ys = y_interp(spot)
    parts.append(f'<line x1="{lblx+10}" y1="{ys:.1f}" x2="{width-96}" y2="{ys:.1f}" stroke="#f0c040" '
                 f'stroke-width="1.5" stroke-dasharray="6 3"/>')
    parts.append(f'<text x="{width-8}" y="{ys+4:.1f}" text-anchor="end" font-size="13" '
                 f'font-weight="600" fill="#f0c040">SPOT {spot:.0f}</text>')
    if gflip and strikes[-1] <= gflip <= strikes[0]:
        yg = y_interp(gflip)
        parts.append(f'<line x1="{lblx+10}" y1="{yg:.1f}" x2="{width-96}" y2="{yg:.1f}" stroke="#58a6ff" '
                     f'stroke-width="1.5" stroke-dasharray="2 3"/>')
        parts.append(f'<text x="{width-8}" y="{yg+4:.1f}" text-anchor="end" font-size="13" '
                     f'font-weight="600" fill="#58a6ff">GFLIP {gflip:.0f}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def _svg_oi_lines(spot, gflip, chain):
    """Line chart of raw OPEN INTEREST by strike — call OI (green) vs put OI (red). Shows the
    contract magnets (round-number strikes like 7700) that pile up huge OI but whose calls & puts
    ~cancel → ~0 net gamma, so they're INVISIBLE on the net-GEX chart above. Strike on X (low→high),
    OI on Y (taller = more open contracts)."""
    lo, hi = spot - 140, spot + 140
    agg = {}                                           # sum OI across expiries per strike (like gex_by_strike)
    for c in chain:
        k = c["strike"]
        if lo <= k <= hi:
            ac, ap = agg.get(k, (0.0, 0.0))
            agg[k] = (ac + c["oi_call"], ap + c["oi_put"])
    pts = sorted((k, cv, pv) for k, (cv, pv) in agg.items())
    if len(pts) < 2:
        return "<p>no OI in window</p>"
    ks = [p[0] for p in pts]
    kmin, kmax = ks[0], ks[-1]
    maxoi = max(max(cv, pv) for _, cv, pv in pts) or 1.0
    W, H, pl, pr, pt, pb = 860, 240, 48, 14, 32, 30

    def X(k): return pl + (k - kmin) / (kmax - kmin) * (W - pl - pr)
    def Y(oi): return H - pb - oi / maxoi * (H - pt - pb)

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" style="max-width:{W}px">']
    for frac in (0.0, 0.5, 1.0):                       # y gridlines + labels (0 / half / max, in k)
        yv = maxoi * frac
        y = Y(yv)
        parts.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{W-pr}" y2="{y:.1f}" stroke="#21262d"/>')
        parts.append(f'<text x="{pl-6}" y="{y+4:.1f}" text-anchor="end" font-size="10" '
                     f'fill="#8b949e">{yv/1000:.0f}k</text>')
    for k in ks:                                       # x strike ticks at round (÷25) strikes
        if k % 25 == 0:
            x = X(k)
            parts.append(f'<line x1="{x:.1f}" y1="{H-pb}" x2="{x:.1f}" y2="{H-pb+4}" stroke="#8b949e"/>')
            parts.append(f'<text x="{x:.1f}" y="{H-pb+16:.1f}" text-anchor="middle" font-size="10" '
                         f'fill="#8b949e">{k:.0f}</text>')
    if kmin <= spot <= kmax:                           # spot + gflip vertical markers
        xs = X(spot)
        parts.append(f'<line x1="{xs:.1f}" y1="{pt}" x2="{xs:.1f}" y2="{H-pb}" stroke="#f0c040" '
                     f'stroke-width="1.5" stroke-dasharray="6 3"/>')
        parts.append(f'<text x="{xs:.1f}" y="{pt-3:.1f}" text-anchor="middle" font-size="10" '
                     f'fill="#f0c040">spot</text>')
    if gflip and kmin <= gflip <= kmax:
        xg = X(gflip)
        parts.append(f'<line x1="{xg:.1f}" y1="{pt}" x2="{xg:.1f}" y2="{H-pb}" stroke="#58a6ff" '
                     f'stroke-width="1.5" stroke-dasharray="2 3"/>')
    put_line = " ".join(f"{X(k):.1f},{Y(pv):.1f}" for k, cv, pv in pts)
    call_line = " ".join(f"{X(k):.1f},{Y(cv):.1f}" for k, cv, pv in pts)
    parts.append(f'<polyline points="{put_line}" fill="none" stroke="{NEG}" stroke-width="2"/>')
    parts.append(f'<polyline points="{call_line}" fill="none" stroke="{POS}" stroke-width="2"/>')
    pk_c = max(pts, key=lambda p: p[1])                # annotate the peak call & put OI strikes
    pk_p = max(pts, key=lambda p: p[2])
    parts.append(f'<circle cx="{X(pk_c[0]):.1f}" cy="{Y(pk_c[1]):.1f}" r="3" fill="{POS}"/>')
    parts.append(f'<text x="{X(pk_c[0]):.1f}" y="{Y(pk_c[1])-6:.1f}" text-anchor="middle" '
                 f'font-size="10" fill="{POS}">{pk_c[0]:.0f} · {pk_c[1]/1000:.1f}k calls</text>')
    parts.append(f'<circle cx="{X(pk_p[0]):.1f}" cy="{Y(pk_p[2]):.1f}" r="3" fill="{NEG}"/>')
    parts.append(f'<text x="{X(pk_p[0]):.1f}" y="{Y(pk_p[2])+15:.1f}" text-anchor="middle" '
                 f'font-size="10" fill="{NEG}">{pk_p[0]:.0f} · {pk_p[2]/1000:.1f}k puts</text>')
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

    # Prefer the fresher regime spot (every ~5 min) over the 30-min chain snapshot, and recompute
    # Gflip/GEX at THAT spot with the (slow-moving) OI chain — so the markers track the live market.
    reg = _latest_regime(date)
    spot_ts = ts
    if reg:
        spot, spot_ts = reg

    gflip = gex.gamma_flip(chain, spot)
    regime = gex.gex_regime(spot, gflip)
    net_total = gex.net_gex(spot, chain) / 1e6
    net_0dte = gex.net_gex_0dte(spot, chain) / 1e6
    calls, puts = gex.gex_ladders(spot, chain, n=6)
    by_strike = gex.gex_by_strike(spot, chain)
    dist = (spot - gflip) / gflip * 100 if gflip else 0

    reg_color = NEG if regime == "negative" else POS
    metrics = [
        ("Spot", f"{spot:.2f}", "#f0c040"),
        ("Gflip", f"{gflip:.2f}", "#58a6ff"),
        ("Distance", f"{spot-gflip:+.1f} ({dist:+.2f}%)", reg_color),
        ("Regime", regime.upper(), reg_color),
        ("Net GEX (total)", f"{net_total:,.0f}M", "#ddd"),
        ("Net GEX (0DTE)", f"{net_0dte:,.0f}M", "#ddd"),
        # Gamma-weighted walls (top of the ladders) — these MATCH the bar chart, unlike the old
        # raw-OI walls (a round-number OI magnet like 7700 can net to ~0 gamma). Raw OI is now
        # its own line chart below.
        ("Call Wall (γ)", f"{calls[0][0]:.0f}" if calls else "—", POS),
        ("Put Wall (γ)", f"{puts[0][0]:.0f}" if puts else "—", NEG),
    ]
    cards = "".join(
        f"<div class='card'><div class='lbl'>{lbl}</div>"
        f"<div class='val' style='color:{c}'>{val}</div></div>" for lbl, val, c in metrics)

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
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
  .ladders{{display:flex;flex-direction:column;gap:14px;align-items:flex-start}}  /* desktop: stacked */
  /* ── Mobile: stack the chart above the ladders, and put the two ladders side by side ── */
  @media (max-width:760px){{
    body{{padding:12px}}
    h1{{font-size:16px}} .sub{{font-size:11px}}
    .cards{{gap:6px}}
    .card{{min-width:0;flex:1 1 28%;padding:8px 10px}}
    .lbl{{font-size:10px}} .val{{font-size:15px}}
    .grid{{flex-direction:column;gap:14px}}
    .chart{{min-width:0;width:100%;box-sizing:border-box;padding:8px}}
    .ladders{{flex-direction:row;gap:8px}}          /* mobile: side by side */
    .ladders table{{flex:1;min-width:0}}
    caption{{padding:8px 8px;font-size:13px}}
    th,td{{padding:5px 8px;font-size:12px}}
  }}
</style></head><body>
<h1>GEX Dashboard — {date}</h1>
<div class="sub">spot as of {spot_ts[11:] if len(spot_ts) > 11 else spot_ts} · OI chain as of {ts[11:] if len(ts) > 11 else ts} (30-min) · auto-refresh 60s · net GEX = our-convention $M (±5% / 3-expiry)</div>
<div class="cards">{cards}</div>
<div class="grid">
  <div class="chart">
    <div style="color:#8b949e;font-size:12px;margin-bottom:8px">
      Net GEX by strike &nbsp;·&nbsp; <span style="color:{NEG}">▉ put support (−)</span>
      &nbsp; <span style="color:{POS}">▉ call resistance (+)</span></div>
    {_svg_profile(spot, gflip, by_strike)}
  </div>
  <div class="ladders">
    {_ladder_table("Support (−GEX)", puts, NEG)}
    {_ladder_table("Resistance (+GEX)", calls, POS)}
  </div>
</div>
<div class="chart" style="margin-top:16px">
  <div style="color:#8b949e;font-size:12px;margin-bottom:8px">
    Open interest by strike &nbsp;·&nbsp; <span style="color:{POS}">▬ call OI</span>
    &nbsp; <span style="color:{NEG}">▬ put OI</span> &nbsp;—&nbsp; the raw contract magnets; a round-number
    strike (e.g. 7700) piles up huge OI but nets to ~0 gamma, so it's invisible in the net-GEX chart above</div>
  {_svg_oi_lines(spot, gflip, chain)}
</div>
<div class="note"><b>Read it:</b> the longest red bars (top chart) are the heaviest put-support nodes (where
dealers defend / price magnets); the longest green bars are call resistance. Spot (yellow) vs Gflip (blue)
sets the regime. The header <b>walls are gamma-weighted (γ)</b> — they match the bars. The <b>OI line chart</b>
below shows raw open contracts: a tall peak (e.g. 7700) is a pin magnet by sheer size even when its net gamma
is ~0 (calls and puts cancel). <b>Runway</b> = enter a PUT <i>below</i> the support cluster or a CALL
<i>above</i> the resistance shelf (room to run); <b>IntoWall</b> = entering into a heavy node. Wait out the
noise zone (the node cluster ± a small buffer) and take the break.</div>
</body></html>"""

    outdir = os.path.join(GEX_DIR, "dashboards")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"dashboard_{date}.html")
    with open(out, "w") as f:
        f.write(html)
    # Stable-name copy so a phone can bookmark ONE URL (…/latest.html) that's always today's board.
    with open(os.path.join(outdir, "latest.html"), "w") as f:
        f.write(html)
    print(f"✅ wrote {out}")
    print(f"   spot {spot:.2f} · Gflip {gflip:.2f} ({dist:+.2f}%, {regime}) · "
          f"net {net_total:,.0f}M · heaviest support {puts[0][0]:.0f} ({puts[0][1]/1e6:,.0f}M)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
