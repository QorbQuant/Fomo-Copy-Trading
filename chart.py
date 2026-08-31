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
    # Time-weighted return: chain the return between consecutive NAV posts,
    # crediting deposits to the basis (never as performance). Immune to deposit
    # timing — a $210 top-up doesn't show as a gain OR a drawdown. This is how
    # funds report strategy performance vs contributions.
    def dep_between(a, b):
        return sum(amt for t, amt in deps if a < t <= b)

    def dep_upto(ts):
        return sum(a for t, a in deps if t <= ts)

    clean = [(ts, nav) for ts, nav in navs
             if dep_upto(ts) > 0 and nav > dep_upto(ts) * 0.5]  # drop feed-outage posts
    if len(clean) < 2:
        raise SystemExit("not enough clean NAV points yet")

    index, series = 1.0, []
    prev_ts, prev_nav = clean[0]
    series.append((prev_ts, 0.0))
    for ts, nav in clean[1:]:
        contrib = dep_between(prev_ts, ts)
        base = prev_nav + contrib
        if base > 0:
            index *= nav / base
        series.append((ts, (index - 1) * 100))
        prev_ts, prev_nav = ts, nav

    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 900, 500, 64, 26, 40, 44
    t0, t1 = series[0][0], series[-1][0]
    vmin = min(v for _, v in series)
    vmax = max(v for _, v in series)
    span = max(vmax - vmin, 1)
    lo = min(0, vmin) - span * 0.08
    hi = vmax + span * 0.08

    def x(ts):
        return PAD_L + (ts - t0) / max(t1 - t0, 1) * (W - PAD_L - PAD_R)

    def y(v):
        return PAD_T + (1 - (v - lo) / (hi - lo)) * (H - PAD_T - PAD_B)

    line = " ".join(f"{x(ts):.1f},{y(v):.1f}" for ts, v in series)
    # nice-ish y ticks
    import math
    step = max(1, round(span / 5))
    step = next(s for s in (1, 2, 5, 10, 20, 25, 50, 100, 200) if s >= step)
    gridlines, ylabels, zero = "", "", ""
    start = math.floor(lo / step) * step
    v = start
    while v <= hi:
        if lo <= v <= hi:
            gy = y(v)
            cls = "grid zero" if v == 0 else "grid"
            gridlines += f'<line x1="{PAD_L}" x2="{W-PAD_R}" y1="{gy:.1f}" y2="{gy:.1f}" class="{cls}"/>'
            ylabels += (f'<text x="{PAD_L-10}" y="{gy+4:.1f}" class="axis" '
                        f'text-anchor="end">{v:+g}%</text>')
        v += step
    xlabels = ""
    for i in range(5):
        ts = t0 + (t1 - t0) * i / 4
        anchor = "start" if i == 0 else ("end" if i == 4 else "middle")
        xlabels += (f'<text x="{x(ts):.1f}" y="{H-14}" class="axis" text-anchor="{anchor}">'
                    f'{time.strftime("%b %-d", time.localtime(ts))}</text>')

    now_pct = series[-1][1]
    pts_json = json.dumps([[round(ts), round(v, 2)] for ts, v in series])

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>avgJOE Vault Return</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html,body {{ margin:0; background:#ffffff; }}
  .wrap {{ font-family: Arial, Helvetica, sans-serif; color:#2a3f5f;
    max-width: 940px; margin: 0 auto; padding: 20px 12px 12px; }}
  .title {{ text-align:center; font-size: 17px; color:#2a3f5f; margin-bottom: 2px; }}
  .now {{ text-align:center; font-size: 13px; color:#888; margin-bottom: 6px; }}
  svg {{ width:100%; height:auto; display:block; }}
  .grid {{ stroke:#eef0f4; stroke-width:1; }}
  .grid.zero {{ stroke:#b0b8c6; stroke-width:1; }}
  .frame {{ fill:none; stroke:#c8ccd4; stroke-width:1; }}
  .axis {{ fill:#7b7b7b; font-size:12px; font-family: Arial, sans-serif; }}
  .pnl {{ fill:none; stroke:#1f77b4; stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }}
  #dot {{ fill:#1f77b4; stroke:#fff; stroke-width:1.5; display:none; }}
  #cross {{ stroke:#c8ccd4; stroke-width:1; stroke-dasharray:3 3; display:none; }}
  #tip {{ position:fixed; pointer-events:none; background:#fff; border:1px solid #c8ccd4;
    border-radius:3px; padding:4px 8px; font:12px Arial; color:#2a3f5f; display:none;
    box-shadow:0 1px 4px rgba(0,0,0,.15); }}
</style></head><body><div class="wrap">
<div class="title">avgJOE Copy Vault &mdash; return</div>
<div class="now">time-weighted, deposit-neutral &middot; now {now_pct:+.1f}%</div>
<svg id="ch" viewBox="0 0 {W} {H}" role="img" aria-label="Vault unrealized return percent over time">
  {gridlines}{ylabels}{xlabels}
  <rect class="frame" x="{PAD_L}" y="{PAD_T}" width="{W-PAD_L-PAD_R}" height="{H-PAD_T-PAD_B}"/>
  <polyline class="pnl" points="{line}"/>
  <line id="cross" y1="{PAD_T}" y2="{H-PAD_B}"/>
  <circle id="dot" r="3.5"/>
  <rect x="{PAD_L}" y="{PAD_T}" width="{W-PAD_L-PAD_R}" height="{H-PAD_T-PAD_B}"
    fill="transparent" id="hit"/>
</svg>
<div id="tip"></div>
<script>
const P={pts_json}, T0={t0}, T1={t1}, XL={PAD_L}, XR={W-PAD_R},
      YT={PAD_T}, YB={H-PAD_B}, LO={lo:.4f}, HI={hi:.4f};
const svg=document.getElementById('ch'), tip=document.getElementById('tip'),
      cross=document.getElementById('cross'), dot=document.getElementById('dot');
const xOf=ts=>XL+(ts-T0)/Math.max(T1-T0,1)*(XR-XL);
const yOf=v=>YT+(1-(v-LO)/(HI-LO))*(YB-YT);
document.getElementById('hit').addEventListener('mousemove',e=>{{
  const r=svg.getBoundingClientRect(), sx=svg.viewBox.baseVal.width/r.width;
  const ts=T0+((e.clientX-r.left)*sx-XL)/(XR-XL)*(T1-T0);
  let b=P[0]; for(const p of P) if(Math.abs(p[0]-ts)<Math.abs(b[0]-ts)) b=p;
  cross.setAttribute('x1',xOf(b[0])); cross.setAttribute('x2',xOf(b[0]));
  dot.setAttribute('cx',xOf(b[0])); dot.setAttribute('cy',yOf(b[1]));
  cross.style.display=dot.style.display='block';
  const d=new Date(b[0]*1000);
  tip.innerHTML=d.toLocaleString(undefined,{{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}})
    +'<br><b>'+(b[1]>=0?'+':'')+b[1].toFixed(1)+'%</b>';
  tip.style.display='block';
  tip.style.left=(e.clientX+14)+'px'; tip.style.top=(e.clientY+10)+'px';
}});
document.getElementById('hit').addEventListener('mouseleave',()=>{{
  tip.style.display=cross.style.display=dot.style.display='none'; }});
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
