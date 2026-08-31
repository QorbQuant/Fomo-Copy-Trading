"""Vault PnL chart: on-chain NAV history vs cumulative deposits.

Every executor postNav() emits NavPosted; every deposit emits Deposit — the
full series lives on-chain. This fetches both, renders a self-contained HTML
chart (inline SVG, hover crosshair, light/dark), and writes vault_pnl.html.

    python3 chart.py
"""

import json
import subprocess
import time

import lib

VAULT = "0x12b508A1883b910a537c25883AE7DB518c1511D9"
RPC = "https://rpc.mainnet.chain.robinhood.com"
CHUNK = 200_000
LOOKBACK_BLOCKS = 3_000_000  # ~3.5 days at 0.1s blocks; widen as history grows


def topic(sig):
    return subprocess.run(["cast", "keccak", sig], capture_output=True,
                          text=True).stdout.strip()


def fetch_events():
    t_nav = topic("NavPosted(uint256)")
    t_dep = topic("Deposit(address,uint256,uint256)")
    head = int(lib.rpc(RPC, "eth_blockNumber", []), 16)
    logs = []
    start = head - LOOKBACK_BLOCKS
    for a in range(start, head + 1, CHUNK):
        b = min(a + CHUNK - 1, head)
        logs += lib.rpc(RPC, "eth_getLogs", [{"address": VAULT,
                        "fromBlock": hex(a), "toBlock": hex(b)}])
    navs, deps = [], []
    for lg in logs:
        ts = lib.block_timestamp(RPC, lg["blockNumber"])
        if lg["topics"][0] == t_nav:
            navs.append((ts, int(lg["data"], 16) / 1e6))
        elif lg["topics"][0] == t_dep:
            assets = int(lg["data"][2:66], 16) / 1e6
            deps.append((ts, assets))
    navs.sort()
    deps.sort()
    return navs, deps


def build_html(navs, deps):
    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 920, 380, 56, 120, 16, 34
    t0 = min(navs[0][0], deps[0][0]) if deps else navs[0][0]
    t1 = navs[-1][0]
    total_dep = sum(d[1] for d in deps)
    # cumulative-deposit step series sampled at nav timestamps + its own steps
    cum, steps = 0.0, []
    for ts, a in deps:
        steps.append((ts, cum))
        cum += a
        steps.append((ts, cum))
    steps.append((t1, cum))
    ymax = max(max(v for _, v in navs), cum) * 1.06
    ymin = 0.0

    def x(ts):
        return PAD_L + (ts - t0) / max(t1 - t0, 1) * (W - PAD_L - PAD_R)

    def y(v):
        return PAD_T + (1 - (v - ymin) / (ymax - ymin)) * (H - PAD_T - PAD_B)

    nav_pts = " ".join(f"{x(ts):.1f},{y(v):.1f}" for ts, v in navs)
    dep_pts = " ".join(f"{x(ts):.1f},{y(v):.1f}" for ts, v in steps)

    gridlines, ylabels = "", ""
    for i in range(1, 5):
        v = ymax * i / 5
        gy = y(v)
        gridlines += (f'<line x1="{PAD_L}" x2="{W-PAD_R}" y1="{gy:.1f}" '
                      f'y2="{gy:.1f}" class="grid"/>')
        ylabels += (f'<text x="{PAD_L-8}" y="{gy+4:.1f}" class="axis" '
                    f'text-anchor="end">${v:,.0f}</text>')
    xlabels = ""
    for i in range(5):
        ts = t0 + (t1 - t0) * i / 4
        anchor = "start" if i == 0 else ("end" if i == 4 else "middle")
        xlabels += (f'<text x="{x(ts):.1f}" y="{H-10}" class="axis" '
                    f'text-anchor="{anchor}">'
                    f'{time.strftime("%b %d %H:%M", time.localtime(ts))}</text>')

    nav_now = navs[-1][1]
    pnl = nav_now - total_dep
    pnl_pct = pnl / total_dep * 100 if total_dep else 0
    pnl_cls = "good" if pnl >= 0 else "bad"
    points_json = json.dumps([[round(ts), round(v, 2)] for ts, v in navs])

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>avgJOE Vault PnL</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  .viz-root {{ color-scheme: light;
    --surface-1:#fcfcfb; --text-primary:#0b0b0b; --text-secondary:#52514e;
    --grid:#e8e7e3; --series-1:#2a78d6; --ref:#8a897f; --good:#008300; --bad:#e34948; }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) .viz-root {{ color-scheme: dark;
      --surface-1:#1a1a19; --text-primary:#ffffff; --text-secondary:#c3c2b7;
      --grid:#33322f; --series-1:#3987e5; --ref:#8a897f; --good:#3fae3f; --bad:#e66767; }} }}
  :root[data-theme="dark"] .viz-root {{ color-scheme: dark;
    --surface-1:#1a1a19; --text-primary:#ffffff; --text-secondary:#c3c2b7;
    --grid:#33322f; --series-1:#3987e5; --ref:#8a897f; --good:#3fae3f; --bad:#e66767; }}
  body {{ margin:0; }}
  .viz-root {{ font: 14px/1.45 -apple-system, system-ui, sans-serif;
    background: var(--surface-1); color: var(--text-primary);
    max-width: 980px; margin: 0 auto; padding: 24px 16px 40px; }}
  h1 {{ font-size: 1.15rem; margin: 0 0 2px; }}
  .sub {{ color: var(--text-secondary); font-size: .85rem; margin-bottom: 18px; }}
  .tiles {{ display: flex; gap: 28px; flex-wrap: wrap; margin-bottom: 18px; }}
  .tile .k {{ color: var(--text-secondary); font-size: .78rem; }}
  .tile .v {{ font-size: 1.5rem; font-weight: 650; font-variant-numeric: tabular-nums; }}
  .tile .v.good {{ color: var(--good); }} .tile .v.bad {{ color: var(--bad); }}
  svg {{ width: 100%; height: auto; display: block; }}
  .grid {{ stroke: var(--grid); stroke-width: 1; }}
  .axis {{ fill: var(--text-secondary); font-size: 11px; }}
  .endlab {{ font-size: 12px; fill: var(--text-primary); font-weight: 600; }}
  .endlab.ref {{ fill: var(--text-secondary); font-weight: 400; }}
  #tip {{ position: fixed; pointer-events: none; background: var(--surface-1);
    border: 1px solid var(--grid); border-radius: 6px; padding: 5px 9px;
    font-size: 12px; display: none; box-shadow: 0 2px 8px rgba(0,0,0,.15); }}
</style></head><body><div class="viz-root">
<h1>avgJOE Copy Vault — NAV vs deposits</h1>
<div class="sub">On-chain NavPosted events, ~10-minute resolution · mirror of @AvgJoesCrypto ·
early-history dips to ~$0 are price-feed outages in NAV posts (since fixed), not losses</div>
<div class="tiles">
  <div class="tile"><div class="k">Vault NAV</div><div class="v">${nav_now:,.2f}</div></div>
  <div class="tile"><div class="k">Deposited</div><div class="v">${total_dep:,.2f}</div></div>
  <div class="tile"><div class="k">PnL</div><div class="v {pnl_cls}">{pnl:+,.2f} ({pnl_pct:+.1f}%)</div></div>
</div>
<svg id="ch" viewBox="0 0 {W} {H}" role="img" aria-label="Vault NAV over time versus cumulative deposits">
  {gridlines}{ylabels}{xlabels}
  <polyline points="{dep_pts}" fill="none" stroke="var(--ref)" stroke-width="1.5"
    stroke-dasharray="5 4"/>
  <polyline points="{nav_pts}" fill="none" stroke="var(--series-1)" stroke-width="2"
    stroke-linejoin="round"/>
  <line id="cross" x1="0" x2="0" y1="{PAD_T}" y2="{H-PAD_B}" class="grid"
    style="display:none"/>
  <circle id="dot" r="4" fill="var(--series-1)" stroke="var(--surface-1)"
    stroke-width="2" style="display:none"/>
  <text class="endlab" x="{x(t1)+8:.1f}" y="{y(nav_now)+4:.1f}">NAV ${nav_now:,.0f}</text>
  <text class="endlab ref" x="{x(t1)+8:.1f}" y="{y(cum)+14:.1f}">deposits ${cum:,.0f}</text>
  <rect x="{PAD_L}" y="{PAD_T}" width="{W-PAD_L-PAD_R}" height="{H-PAD_T-PAD_B}"
    fill="transparent" id="hit"/>
</svg>
<div id="tip"></div>
<script>
const P = {points_json}, T0 = {t0}, T1 = {t1},
      XL = {PAD_L}, XR = {W - PAD_R}, YT = {PAD_T}, YB = {H - PAD_B},
      YMAX = {ymax:.4f}, DEP = {total_dep:.2f};
const svg = document.getElementById('ch'), tip = document.getElementById('tip'),
      cross = document.getElementById('cross'), dot = document.getElementById('dot');
const xOf = ts => XL + (ts - T0) / Math.max(T1 - T0, 1) * (XR - XL);
const yOf = v => YT + (1 - v / YMAX) * (YB - YT);
document.getElementById('hit').addEventListener('mousemove', e => {{
  const r = svg.getBoundingClientRect(), sx = svg.viewBox.baseVal.width / r.width;
  const mx = (e.clientX - r.left) * sx;
  const ts = T0 + (mx - XL) / (XR - XL) * (T1 - T0);
  let best = P[0];
  for (const p of P) if (Math.abs(p[0] - ts) < Math.abs(best[0] - ts)) best = p;
  cross.setAttribute('x1', xOf(best[0])); cross.setAttribute('x2', xOf(best[0]));
  dot.setAttribute('cx', xOf(best[0])); dot.setAttribute('cy', yOf(best[1]));
  cross.style.display = dot.style.display = 'block';
  const d = new Date(best[0] * 1000);
  const pnl = best[1] - DEP;
  tip.innerHTML = d.toLocaleString(undefined, {{month:'short', day:'numeric',
    hour:'2-digit', minute:'2-digit'}}) +
    '<br><b>NAV $' + best[1].toLocaleString() + '</b><br>PnL ' +
    (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
  tip.style.display = 'block';
  tip.style.left = (e.clientX + 14) + 'px'; tip.style.top = (e.clientY + 10) + 'px';
}});
document.getElementById('hit').addEventListener('mouseleave', () => {{
  tip.style.display = cross.style.display = dot.style.display = 'none'; }});
</script>
</div></body></html>"""


def main():
    navs, deps = fetch_events()
    if not navs:
        raise SystemExit("no NavPosted events found")
    html = build_html(navs, deps)
    out = lib.ROOT / "vault_pnl.html"
    out.write_text(html)
    print(f"{len(navs)} NAV points, {len(deps)} deposits -> {out}")


if __name__ == "__main__":
    main()
