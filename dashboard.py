"""Live mirror dashboard: AJC's fomo account vs the avgJOE vault.

Gathers a full snapshot — trader portfolio (all chains), vault NAV + positions
+ per-network holdings, recent activity on both sides — and renders a
self-contained HTML monitor (dashboard.html). Run on the VM (live logs +
mainnet access); the HTML embeds the snapshot.

    python3 dashboard.py
"""

import html
import json
import time
from datetime import datetime, timezone

import lib
import sleeve
from executor import Executor, USDG, uint
from sync import fetch_trader_portfolio

SOL_MINT = "So11111111111111111111111111111111111111112"


def gather(cfg):
    ex = Executor(mainnet=True)
    V = ex.dep["vault"]
    d = lib.data_dir(cfg)

    # ---- trader (all chains, already merged by fetch_trader_portfolio) ----
    positions, tcash, ttotal, _ = fetch_trader_portfolio(cfg)
    tnet = {}
    for p in positions:
        tnet[p.get("chain", "robinhood")] = tnet.get(p.get("chain", "robinhood"), 0) + p["usd"]
    trader = {
        "value": ttotal,
        "cash": tcash,
        "positions": sorted(positions, key=lambda p: -p["usd"]),
        "networks": tnet,
    }
    tw = {}
    for p in positions:
        tw[p["addr"].lower()] = tw.get(p["addr"].lower(), 0) + p["usd"] / ttotal

    # ---- vault ----
    usdg = uint(ex.call(USDG, "balanceOf(address)(uint256)", V)) / 1e6
    vpos, vnav = [], usdg
    n = uint(ex.call(V, "heldTokensLength()(uint256)"))
    for i in range(n):
        a = ex.call(V, "heldTokens(uint256)(address)", i).strip()
        bal = uint(ex.call(a, "balanceOf(address)(uint256)", V))
        dec = uint(ex.call(a, "decimals()(uint8)"))
        info = lib.token_price_info(a, "robinhood")
        usd = bal / 10 ** dec * (info["price"] or 0)
        vnav += usd
        vpos.append({"addr": a.lower(), "symbol": info["symbol"] or a[:8], "usd": usd,
                     "chain": "robinhood"})
    # solana sleeve — itemized so the dashboard shows WHAT is in it, not a lump
    pub = sleeve._env().get("SLEEVE_SOLANA_PUBKEY")
    sleeve_items = sleeve.sleeve_holdings(cfg, pub) if pub else []
    sleeve_usd = sum(h["usd"] for h in sleeve_items)
    vnav += sleeve_usd
    vnet = {"robinhood": vnav - usdg - sleeve_usd + usdg, "solana": sleeve_usd}
    vnet = {"robinhood": (vnav - sleeve_usd), "solana": sleeve_usd}
    vw = {p["addr"]: p["usd"] / vnav for p in vpos}
    vpos.sort(key=lambda p: -p["usd"])

    # ---- merged weight comparison ----
    keys = {}
    for p in positions:
        keys[p["addr"].lower()] = p["symbol"]
    for p in vpos:
        keys.setdefault(p["addr"], p["symbol"])
    rows = []
    for k, sym in keys.items():
        rows.append({"symbol": sym, "trader": tw.get(k, 0) * 100, "vault": vw.get(k, 0) * 100})
    rows.sort(key=lambda r: -max(r["trader"], r["vault"]))
    coverage = sum(min(r["trader"], r["vault"]) for r in rows)  # % overlap

    # ---- PnL (time-weighted, from on-chain NavPosted history is heavy; use
    #      nav.json + deposits from executions is unavailable here — approximate
    #      with the chart's series if present) ----
    pnl = None
    try:
        import chart
        navs, deps = chart.fetch_events()
        dep_total = sum(a for _, a in deps)
        # time-weighted chain
        clean = [(t, v) for t, v in navs if sum(a for tt, a in deps if tt <= t) > 0
                 and v > sum(a for tt, a in deps if tt <= t) * 0.5]
        idx = 1.0
        if len(clean) >= 2:
            pt, pv = clean[0]
            for t, v in clean[1:]:
                base = pv + sum(a for tt, a in deps if pt < tt <= t)
                if base > 0:
                    idx *= v / base
                pt, pv = t, v
        pnl = {"twr_pct": (idx - 1) * 100, "deposits": dep_total, "nav": vnav}
    except Exception:
        pass

    # ---- recent activity ----
    def recent(path, key, n=14):
        rows = lib.read_jsonl(d / path)[-n:]
        return list(reversed(rows))

    trades = recent("trades.jsonl", "detected_at")
    trades = [t for t in lib.read_jsonl(d / "trades.jsonl")
              if t.get("kind") == "swap" and not t.get("backfill")][-16:]
    trades = list(reversed(trades))
    execs = list(reversed(lib.read_jsonl(d / "executions_mainnet.jsonl")))[:16]

    # ---- watchlist trader simulations (paper vaults) + their REAL holdings ----
    import simulate as sim_mod
    sims = sim_mod.simulate_all(cfg)
    for s in sims:
        try:
            # same book fetcher as AJC (incl. anti-poisoning + trusted-token
            # hardening), pointed at the watchlist trader's addresses
            wcfg = dict(cfg)
            wcfg["trader"] = cfg["traders"][s["key"]]
            wpos, wcash, wtotal, _ = fetch_trader_portfolio(wcfg)
            s["book"] = {"positions": sorted(wpos, key=lambda p: -p["usd"])[:8],
                         "cash": wcash, "total": wtotal}
        except Exception as e:
            print(f"  [dash warn] {s['key']} book fetch failed: {str(e)[:100]}")
            s["book"] = None

    return {
        "ts": time.time(),
        "trader": trader,
        "vault": {"nav": vnav, "cash": usdg, "sleeve": sleeve_usd,
                  "sleeve_items": sleeve_items, "positions": vpos,
                  "networks": vnet, "address": V},
        "compare": {"rows": rows, "coverage": coverage},
        "pnl": pnl,
        "trades": trades,
        "execs": execs,
        "sims": sims,
    }


# --------------------------------------------------------------------- render

def esc(s):
    return html.escape(str(s))


def fmt_usd(v, dp=0):
    return f"${v:,.{dp}f}"


def ago(ts):
    s = time.time() - ts
    if s < 90:
        return f"{int(s)}s"
    if s < 5400:
        return f"{int(s/60)}m"
    if s < 172800:
        return f"{int(s/3600)}h"
    return f"{int(s/86400)}d"


NET_LABEL = {"robinhood": "Robinhood", "solana": "Solana", "base": "Base",
             "monad": "Monad", "bnb": "BNB", "ethereum": "Ethereum"}


def nav_svg(series, aum, w=560, h=110):
    """Inline SVG % chart of a sim NAV series (like the vault uPNL chart):
    area+line vs a 0% baseline, endpoint emphasized. Self-contained, theme-aware
    via currentColor on the accent."""
    if len(series) < 2:
        return '<div class="nochart">not enough data for a chart yet</div>'
    t0, t1 = series[0][0], series[-1][0]
    span = max(t1 - t0, 1)
    pcts = [(v / aum - 1) * 100 for _, v in series]
    lo, hi = min(pcts + [0]), max(pcts + [0])
    pad = max((hi - lo) * 0.15, 0.05)
    lo, hi = lo - pad, hi + pad
    X = lambda ts: 4 + (ts - t0) / span * (w - 8)
    Y = lambda p: 4 + (hi - p) / (hi - lo) * (h - 8)
    pts = [(X(ts), Y((v / aum - 1) * 100)) for ts, v in series]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{pts[0][0]:.1f},{Y(0):.1f} " + line + f" {pts[-1][0]:.1f},{Y(0):.1f}"
    zero = Y(0)
    ex, ey = pts[-1]
    return f'''<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" class="navchart" role="img" aria-label="simulated NAV percent chart">
      <line x1="4" y1="{zero:.1f}" x2="{w-4}" y2="{zero:.1f}" class="zeroline"/>
      <polygon points="{area}" class="navarea"/>
      <polyline points="{line}" class="navline" fill="none"/>
      <circle cx="{ex:.1f}" cy="{ey:.1f}" r="3" class="navdot"/>
    </svg>'''


def _book_rows(book):
    """The watchlist trader's REAL current holdings (their fomo book)."""
    if not book:
        return '<div class="simempty">holdings unavailable this refresh</div>'
    rows = ""
    for p in book["positions"]:
        w = p["usd"] / book["total"] * 100 if book["total"] else 0
        rows += (f'<div class="prow"><span class="psym">{esc(p["symbol"])}</span>'
                 f'<span class="pnet">{esc(NET_LABEL.get(p.get("chain", "robinhood"), p.get("chain")))}</span>'
                 f'<span class="pval">{fmt_usd(p["usd"])}</span>'
                 f'<span class="pw">{w:.1f}%</span></div>')
    if book["cash"] > 0.5:
        w = book["cash"] / book["total"] * 100 if book["total"] else 0
        rows += (f'<div class="prow"><span class="psym">cash/stables</span><span class="pnet"></span>'
                 f'<span class="pval">{fmt_usd(book["cash"])}</span>'
                 f'<span class="pw">{w:.1f}%</span></div>')
    return rows or '<div class="simempty">no priced positions</div>'


def sim_section(sims):
    if not sims:
        return ""
    cards = ""
    for s in sims:
        handle = esc(s.get("handle", s["key"]))
        pt, p1, p24 = s["perf_total"], s["perf_1h"], s["perf_24h"]
        cls = lambda p: "up" if p >= 0 else "down"
        book = s.get("book")
        book_total = fmt_usd(book["total"]) if book else "—"
        if s["n_copied"] == 0:
            body = (f'<div class="simempty">sim collecting data &mdash; {s["n_signals"]} signals seen, '
                    f'{s["n_skipped"]} skipped (below the copy floor), no copyable trades yet</div>')
        else:
            rows = ""
            for c in s["contrib"][:7]:
                pcls = "up" if c["pnl"] >= 0 else "down"
                state = "" if c["open"] else " (closed)"
                rows += (f'<div class="prow"><span class="psym">{esc(c["symbol"])}{state}</span>'
                         f'<span class="pnet">{esc(NET_LABEL.get(c["chain"], c["chain"]))}</span>'
                         f'<span class="pval">{fmt_usd(c["value"])}</span>'
                         f'<span class="pw {pcls}">{c["pnl"]:+,.0f}</span></div>')
            body = f'''{nav_svg(s["series"], s["aum"])}
      <div class="simtiles">
        <div><span class="k">1h</span><span class="v {cls(p1)}">{p1:+.2f}%</span></div>
        <div><span class="k">24h</span><span class="v {cls(p24)}">{p24:+.2f}%</span></div>
        <div><span class="k">total</span><span class="v {cls(pt)}">{pt:+.2f}%</span></div>
        <div><span class="k">trades</span><span class="v mono">{s["n_copied_24h"]}<i>/24h</i> {s["n_copied"]}<i>&nbsp;all</i></span></div>
        <div><span class="k">positions</span><span class="v mono">{len(s["positions"])}</span></div>
      </div>
      <div class="stitle" style="margin-top:10px">contribution &mdash; what is driving the sim</div>
      {rows}'''
        cards += f'''
  <div class="panel sim">
    <div class="phead"><span class="dot s"></span><h2>@{handle} &mdash; simulated vault</h2>
      <span class="big">{fmt_usd(s["nav"])}</span></div>
    <div class="sub simsub">paper: ${s["aum"]:,.0f} start, sized like the live vault &middot;
      {s["n_skipped"]} signals below the copy floor</div>
    {body}
    <div class="stitle" style="margin-top:12px">@{handle}'s real holdings &mdash; {book_total}</div>
    {_book_rows(book)}
  </div>'''
    return f'''
<section class="compare">
  <div class="stitle">Watchlist &mdash; simulated vaults (paper, no funds)</div>
  <div class="simblurb">Each watchlist trader gets a card that replays their observed
  fomo trades through a <b>$100K paper vault</b> (same sizing logic as the live one):
  a NAV % chart vs a 0% baseline, 1h / 24h / total performance, trade counts (24h and
  all-time), and positions + contribution &mdash; which symbols are driving the sim's
  PnL, realized + unrealized, sorted by impact. Below the sim: the trader's
  <b>real current holdings</b> read from chain. No funds move; these build the track
  record for a future vault per trader.</div>
  <div class="grid">{cards}</div>
</section>'''


def render(s):
    t, v, c = s["trader"], s["vault"], s["compare"]
    updated = datetime.fromtimestamp(s["ts"], timezone.utc).strftime("%b %d, %H:%M UTC")

    # summary tiles
    pnl = s["pnl"]
    pnl_cell = ""
    if pnl:
        cls = "up" if pnl["twr_pct"] >= 0 else "down"
        pnl_cell = f'<div class="tile"><div class="k">Vault return</div>' \
                   f'<div class="v {cls}">{pnl["twr_pct"]:+.1f}%</div>' \
                   f'<div class="sub">time-weighted</div></div>'

    # weight comparison bars
    bars = ""
    for r in c["rows"]:
        if max(r["trader"], r["vault"]) < 0.4:
            continue
        drift = abs(r["trader"] - r["vault"])
        dcls = "ok" if drift < 1.5 else ("warn" if drift < 5 else "bad")
        bars += f"""
        <div class="wrow">
          <div class="wsym">{esc(r['symbol'])}</div>
          <div class="wbars">
            <div class="wbar trader"><span style="width:{min(r['trader'],100):.1f}%"></span></div>
            <div class="wbar vault"><span style="width:{min(r['vault'],100):.1f}%"></span></div>
          </div>
          <div class="wnums">
            <span class="tw">{r['trader']:.1f}%</span>
            <span class="vw">{r['vault']:.1f}%</span>
            <span class="drift {dcls}">±{drift:.1f}</span>
          </div>
        </div>"""

    # network chips
    def chips(net, total, accent):
        out = ""
        for k in sorted(net, key=lambda k: -net[k]):
            if net[k] < 0.5:
                continue
            pct = net[k] / total * 100 if total else 0
            out += f'<span class="chip {accent}">{esc(NET_LABEL.get(k,k))} ' \
                   f'<b>{pct:.0f}%</b></span>'
        return out or '<span class="chip muted">—</span>'

    # trader positions
    tpos = ""
    for p in t["positions"][:8]:
        w = p["usd"] / t["value"] * 100 if t["value"] else 0
        tpos += f'<div class="prow"><span class="psym">{esc(p["symbol"])}</span>' \
                f'<span class="pnet">{esc(NET_LABEL.get(p.get("chain","robinhood"),p.get("chain")))}</span>' \
                f'<span class="pval">{fmt_usd(p["usd"])}</span>' \
                f'<span class="pw">{w:.1f}%</span></div>'

    # vault positions
    vpos = ""
    for p in v["positions"][:8]:
        w = p["usd"] / v["nav"] * 100 if v["nav"] else 0
        vpos += f'<div class="prow"><span class="psym">{esc(p["symbol"])}</span>' \
                f'<span class="pnet">{esc(NET_LABEL.get(p.get("chain","robinhood"),p.get("chain")))}</span>' \
                f'<span class="pval">{fmt_usd(p["usd"])}</span>' \
                f'<span class="pw">{w:.1f}%</span></div>'
    # sleeve, itemized: every Solana holding on its own row (was one opaque lump)
    for h in v.get("sleeve_items", []):
        if h["usd"] < 0.5:
            continue
        w = h["usd"] / v["nav"] * 100 if v["nav"] else 0
        vpos += f'<div class="prow"><span class="psym">{esc(h["symbol"])}</span>' \
                f'<span class="pnet">Solana &middot; sleeve</span>' \
                f'<span class="pval">{fmt_usd(h["usd"], 2)}</span>' \
                f'<span class="pw">{w:.1f}%</span></div>'

    # activity feed (interleave trader detections + vault executions)
    feed = []
    for x in s["trades"]:
        a = x["asset_token"]
        feed.append((x.get("detected_at", 0), "trader", x.get("chain", "robinhood"),
                     x["side"], a["symbol"], x.get("usd_value") or 0, None))
    for e in s["execs"]:
        if e.get("kind") in ("bridge", "bridge_back", "rotation_bridge"):
            feed.append((e.get("ts", 0), "vault", "bridge", "bridge", e.get("symbol", ""),
                         e.get("usd", 0), e.get("vault_tx")))
        elif e.get("kind") == "skip":
            feed.append((e.get("ts", 0), "vault", "robinhood", "skip",
                         e.get("symbol", ""), 0, None))
        elif "vault_tx" in e or "tx" in e:
            feed.append((e.get("ts", 0), "vault", e.get("chain", e.get("env", "robinhood")),
                         e.get("side", "trade"), e.get("symbol", ""), e.get("usd", 0),
                         e.get("vault_tx") or e.get("tx")))
    feed.sort(key=lambda r: -r[0])
    feed_html = ""
    for ts, who, net, side, sym, usd, tx in feed[:22]:
        sidecls = "buy" if side == "buy" else ("sell" if side == "sell" else "op")
        val = fmt_usd(usd) if usd else ("—" if side in ("skip",) else "")
        feed_html += f"""
        <div class="ev {who}">
          <span class="et">{ago(ts)}</span>
          <span class="ewho {who}">{who}</span>
          <span class="eside {sidecls}">{esc(side)}</span>
          <span class="esym">{esc(sym)}</span>
          <span class="enet">{esc(NET_LABEL.get(net,net))}</span>
          <span class="eval">{val}</span>
        </div>"""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>AJC vs Vault</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#f5f6f8; --panel:#ffffff; --panel2:#f0f1f4; --line:#e2e4e9;
  --ink:#12151c; --ink2:#5a6072; --ink3:#8a90a0;
  --trader:#e08a1e; --trader-dim:#f6e6cf; --vault:#0e9591; --vault-dim:#d3ecea;
  --sim:#7c5cd6;
  --up:#0a8a4a; --down:#d6483b; --warn:#c98a10;
  --mono:"IBM Plex Mono", ui-monospace, monospace;
  --sans:"IBM Plex Sans", system-ui, sans-serif;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg:#0c0e13; --panel:#14171f; --panel2:#1b1f29; --line:#262b37;
  --ink:#eef0f4; --ink2:#a2a9ba; --ink3:#6b7284;
  --trader:#f2a63d; --trader-dim:#33280f; --vault:#2bc0bb; --vault-dim:#0d2b2a;
  --sim:#a08aec;
  --up:#3fbb6f; --down:#f2675a; --warn:#e0a52a;
}} }}
:root[data-theme="dark"] {{
  --bg:#0c0e13; --panel:#14171f; --panel2:#1b1f29; --line:#262b37;
  --ink:#eef0f4; --ink2:#a2a9ba; --ink3:#6b7284;
  --trader:#f2a63d; --trader-dim:#33280f; --vault:#2bc0bb; --vault-dim:#0d2b2a;
  --sim:#a08aec;
  --up:#3fbb6f; --down:#f2675a; --warn:#e0a52a;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font-family:var(--sans);
  font-size:14px; line-height:1.5; -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1080px; margin:0 auto; padding:26px 18px 60px; }}
.mono {{ font-family:var(--mono); font-variant-numeric:tabular-nums; }}
header {{ display:flex; justify-content:space-between; align-items:baseline;
  flex-wrap:wrap; gap:8px; margin-bottom:20px; }}
h1 {{ font-size:1.35rem; font-weight:700; margin:0; letter-spacing:-.01em; }}
h1 .a {{ color:var(--trader); }} h1 .b {{ color:var(--vault); }}
.updated {{ color:var(--ink3); font-size:.8rem; font-family:var(--mono); }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:12px; margin-bottom:22px; }}
.tile {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
  padding:14px 16px; }}
.tile .k {{ color:var(--ink2); font-size:.72rem; text-transform:uppercase;
  letter-spacing:.06em; }}
.tile .v {{ font-family:var(--mono); font-size:1.6rem; font-weight:600; margin-top:3px;
  letter-spacing:-.02em; }}
.tile .v.up {{ color:var(--up); }} .tile .v.down {{ color:var(--down); }}
.tile .sub {{ color:var(--ink3); font-size:.74rem; margin-top:1px; }}
.tile.cov .v {{ color:var(--vault); }}
.grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
@media (max-width:720px) {{ .grid {{ grid-template-columns:1fr; }} }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:14px;
  padding:16px 16px 8px; }}
.phead {{ display:flex; align-items:center; gap:8px; margin-bottom:6px; }}
.dot {{ width:9px; height:9px; border-radius:50%; }}
.dot.t {{ background:var(--trader); }} .dot.v {{ background:var(--vault); }}
.phead h2 {{ font-size:.95rem; margin:0; font-weight:600; }}
.phead .big {{ margin-left:auto; font-family:var(--mono); font-size:1.15rem;
  font-weight:600; }}
.nets {{ display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 12px; }}
.chip {{ font-size:.72rem; font-family:var(--mono); padding:3px 8px; border-radius:20px;
  background:var(--panel2); color:var(--ink2); border:1px solid var(--line); }}
.chip b {{ color:var(--ink); font-weight:600; }}
.chip.t b {{ color:var(--trader); }} .chip.v b {{ color:var(--vault); }}
.prow {{ display:grid; grid-template-columns:1fr auto auto auto; gap:10px;
  align-items:baseline; padding:5px 0; border-top:1px solid var(--line);
  font-family:var(--mono); font-size:.82rem; }}
.prow:first-of-type {{ border-top:0; }}
.psym {{ font-family:var(--sans); font-weight:600; }}
.pnet {{ color:var(--ink3); font-size:.72rem; }}
.pval {{ color:var(--ink2); }}
.pw {{ font-weight:600; min-width:44px; text-align:right; }}
section.compare {{ margin:22px 0; }}
.stitle {{ font-size:.78rem; text-transform:uppercase; letter-spacing:.07em;
  color:var(--ink2); margin:0 0 12px; display:flex; gap:14px; align-items:center; }}
.legend {{ display:flex; gap:12px; margin-left:auto; font-family:var(--mono);
  font-size:.72rem; text-transform:none; letter-spacing:0; }}
.legend i {{ width:10px; height:10px; border-radius:2px; display:inline-block;
  vertical-align:-1px; margin-right:4px; }}
.wrow {{ display:grid; grid-template-columns:78px 1fr 150px; gap:12px;
  align-items:center; padding:7px 0; border-top:1px solid var(--line); }}
.wsym {{ font-weight:600; font-size:.85rem; }}
.wbars {{ display:flex; flex-direction:column; gap:3px; }}
.wbar {{ height:7px; background:var(--panel2); border-radius:4px; overflow:hidden; }}
.wbar span {{ display:block; height:100%; border-radius:4px; }}
.wbar.trader span {{ background:var(--trader); }}
.wbar.vault span {{ background:var(--vault); }}
.wnums {{ font-family:var(--mono); font-size:.76rem; display:flex; gap:8px;
  justify-content:flex-end; }}
.wnums .tw {{ color:var(--trader); }} .wnums .vw {{ color:var(--vault); }}
.wnums .drift {{ min-width:40px; text-align:right; }}
.drift.ok {{ color:var(--up); }} .drift.warn {{ color:var(--warn); }}
.drift.bad {{ color:var(--down); }}
.feed {{ margin-top:6px; }}
.ev {{ display:grid; grid-template-columns:42px 58px 46px 1fr auto auto; gap:10px;
  align-items:baseline; padding:6px 0; border-top:1px solid var(--line);
  font-family:var(--mono); font-size:.8rem; }}
.et {{ color:var(--ink3); }}
.ewho {{ font-size:.68rem; text-transform:uppercase; letter-spacing:.04em;
  padding:1px 6px; border-radius:5px; text-align:center; }}
.ewho.trader {{ background:var(--trader-dim); color:var(--trader); }}
.ewho.vault {{ background:var(--vault-dim); color:var(--vault); }}
.eside {{ font-weight:600; }}
.eside.buy {{ color:var(--up); }} .eside.sell {{ color:var(--down); }}
.eside.op {{ color:var(--ink2); }}
.esym {{ font-family:var(--sans); font-weight:600; }}
.enet {{ color:var(--ink3); font-size:.72rem; }}
.eval {{ text-align:right; color:var(--ink2); }}
footer {{ margin-top:26px; color:var(--ink3); font-size:.74rem; font-family:var(--mono);
  text-align:center; }}
.dot.s {{ background:var(--sim); }}
.panel.sim .big {{ color:var(--sim); }}
.simsub {{ color:var(--ink3); font-size:.74rem; margin:-2px 0 10px; }}
.navchart {{ width:100%; height:110px; display:block; margin:4px 0 10px; }}
.navline {{ stroke:var(--sim); stroke-width:2; stroke-linejoin:round; }}
.navarea {{ fill:var(--sim); opacity:.12; }}
.navdot {{ fill:var(--sim); }}
.zeroline {{ stroke:var(--line); stroke-width:1; stroke-dasharray:3 3; }}
.nochart, .simempty {{ color:var(--ink3); font-size:.8rem; font-family:var(--mono);
  padding:14px 0 10px; }}
.simblurb {{ color:var(--ink2); font-size:.82rem; line-height:1.55; max-width:68ch;
  margin:-4px 0 14px; }}
.simblurb b {{ color:var(--ink); }}
.simtiles {{ display:flex; flex-wrap:wrap; gap:14px; margin:2px 0 4px; }}
.simtiles > div {{ display:flex; flex-direction:column; }}
.simtiles .k {{ color:var(--ink2); font-size:.68rem; text-transform:uppercase;
  letter-spacing:.06em; }}
.simtiles .v {{ font-family:var(--mono); font-size:1.02rem; font-weight:600; }}
.simtiles .v i {{ font-style:normal; color:var(--ink3); font-size:.68rem; }}
.simtiles .v.up, .pw.up {{ color:var(--up); }}
.simtiles .v.down, .pw.down {{ color:var(--down); }}
a {{ color:var(--vault); }}
</style></head><body><div class="wrap">
<header>
  <h1><span class="a">AJC</span> &nbsp;&#8644;&nbsp; <span class="b">Vault</span></h1>
  <div class="updated">updated {esc(updated)} &middot; @AvgJoesCrypto</div>
</header>

<div class="tiles">
  <div class="tile"><div class="k">Trader book</div>
    <div class="v mono" style="color:var(--trader)">{fmt_usd(t['value'])}</div>
    <div class="sub">{len(t['positions'])} positions, {len(t['networks'])} networks</div></div>
  <div class="tile"><div class="k">Vault NAV</div>
    <div class="v mono" style="color:var(--vault)">{fmt_usd(v['nav'])}</div>
    <div class="sub">{len(v['positions'])} positions + sleeve</div></div>
  <div class="tile cov"><div class="k">Weight coverage</div>
    <div class="v mono">{c['coverage']:.0f}%</div>
    <div class="sub">overlap with trader book</div></div>
  {pnl_cell}
</div>

<div class="grid">
  <div class="panel">
    <div class="phead"><span class="dot t"></span><h2>AJC &mdash; fomo</h2>
      <span class="big" style="color:var(--trader)">{fmt_usd(t['value'])}</span></div>
    <div class="nets">{chips(t['networks'], t['value'], 't')}</div>
    {tpos}
  </div>
  <div class="panel">
    <div class="phead"><span class="dot v"></span><h2>avgJOE Vault</h2>
      <span class="big" style="color:var(--vault)">{fmt_usd(v['nav'])}</span></div>
    <div class="nets">{chips(v['networks'], v['nav'], 'v')}</div>
    {vpos}
  </div>
</div>

<section class="compare">
  <div class="stitle">Position weights &mdash; trader vs vault
    <span class="legend"><span><i style="background:var(--trader)"></i>trader</span>
    <span><i style="background:var(--vault)"></i>vault</span>
    <span>&plusmn; drift</span></span></div>
  {bars}
</section>

<section class="compare">
  <div class="stitle">Live activity &mdash; what AJC did, what the vault did</div>
  <div class="feed">{feed_html or '<div class="ev"><span class="et">&mdash;</span><span></span><span></span><span class="esym">no recent activity</span></div>'}</div>
</section>

{sim_section(s.get("sims", []))}

<footer>vault {esc(v['address'][:10])}&hellip;{esc(v['address'][-6:])} on Robinhood Chain &middot;
mirroring across {', '.join(NET_LABEL.get(k,k) for k in sorted(set(list(t['networks'])+list(v['networks']))))}</footer>
</div></body></html>"""


def main():
    cfg = lib.load_config()
    s = gather(cfg)
    (lib.ROOT / "dashboard.html").write_text(render(s))
    print(f"dashboard.html written (trader ${s['trader']['value']:,.0f}, "
          f"vault ${s['vault']['nav']:,.0f}, coverage {s['compare']['coverage']:.0f}%)")


if __name__ == "__main__":
    main()
