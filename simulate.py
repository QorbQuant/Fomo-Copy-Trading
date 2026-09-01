"""Paper-vault simulation for watchlist traders (frankdegods, orangie, ...).

Replays a trader's copy_trades_<key>.jsonl — the same records the live executor
would act on — through a hypothetical vault: start with SIM_AUM cash, buys spend
cash at the recorded fill price, sells return cash FIFO. Produces what the
dashboard needs per trader:

  nav series   [(ts, nav_usd)] — one point per fill, marked at fill prices,
               plus a live point marked at current dexscreener prices
  perf         total / 1h / 24h % (deposit-free, so plain % change is honest)
  trades       copied + skipped counts (total and 24h)
  positions    open positions + per-symbol PnL contribution (realized+unrealized)

Nothing here touches a chain beyond price lookups.
"""

import time
from collections import defaultdict, deque

import lib

SIM_AUM = 100_000.0


def simulate(cfg, key):
    d = lib.data_dir(cfg)
    records = lib.read_jsonl(d / f"copy_trades_{key}.jsonl")
    copies = [r for r in records if r.get("action") == "copy"
              and r.get("fill_price_usd") and r.get("copy_amount")]
    skips = [r for r in records if r.get("action") == "skip"]
    now = time.time()

    out = {
        "key": key,
        "aum": SIM_AUM,
        "n_copied": len(copies),
        "n_skipped": len(skips),
        "n_copied_24h": sum(1 for r in copies if now - (r.get("block_time") or 0) < 86400),
        "n_skipped_24h": sum(1 for r in skips if now - (r.get("block_time") or r.get("detected_at") or 0) < 86400),
        "n_signals": len(records),
        "series": [],
        "nav": SIM_AUM,
        "perf_total": 0.0, "perf_1h": 0.0, "perf_24h": 0.0,
        "positions": [], "contrib": [],
    }
    if not copies:
        return out

    cash = SIM_AUM
    lots = defaultdict(deque)      # (chain, addr) -> deque[[amount, price]]
    last_px = {}                   # (chain, addr) -> last seen price
    meta = {}                      # (chain, addr) -> {symbol, dex_chain}
    realized = defaultdict(float)  # (chain, addr) -> usd

    def mark(prices):
        v = cash
        for k2, dq in lots.items():
            amt = sum(l[0] for l in dq)
            if amt > 1e-12:
                v += amt * prices.get(k2, last_px.get(k2, 0))
        return v

    series = []
    for r in sorted(copies, key=lambda r: r.get("block_time") or r.get("detected_at") or 0):
        k2 = (r.get("chain", "robinhood"), r["asset_address"])
        px = r["fill_price_usd"]
        meta.setdefault(k2, {"symbol": r.get("asset_symbol") or "?",
                             "dex_chain": r.get("dex_chain", k2[0])})
        last_px[k2] = px
        if r["side"] == "buy":
            spend = min(r.get("copy_usd") or 0, cash)
            if spend <= 0:
                continue
            cash -= spend
            lots[k2].append([spend / px, px])
        else:
            qty = r["copy_amount"]
            dq = lots[k2]
            while qty > 1e-12 and dq:
                lot = dq[0]
                take = min(qty, lot[0])
                cash += take * px
                realized[k2] += take * (px - lot[1])
                lot[0] -= take
                qty -= take
                if lot[0] <= 1e-12:
                    dq.popleft()
            # remainder = trader sold what the sim never bought — ignored
        series.append((r.get("block_time") or r.get("detected_at") or now, mark({})))

    # live point: mark open positions at current prices (fallback: last fill)
    live_px = {}
    for k2, dq in lots.items():
        if sum(l[0] for l in dq) > 1e-12:
            px = lib.price_usd(k2[1], meta[k2]["dex_chain"])
            live_px[k2] = px if px else last_px.get(k2, 0)
    nav_now = mark(live_px)
    series.append((now, nav_now))

    def nav_at(cutoff):
        prior = [v for ts, v in series if ts <= cutoff]
        return prior[-1] if prior else series[0][1]

    out["series"] = [(round(ts, 1), round(v, 2)) for ts, v in series]
    out["nav"] = nav_now
    out["perf_total"] = (nav_now / SIM_AUM - 1) * 100
    base_1h, base_24h = nav_at(now - 3600), nav_at(now - 86400)
    out["perf_1h"] = (nav_now / base_1h - 1) * 100 if base_1h else 0.0
    out["perf_24h"] = (nav_now / base_24h - 1) * 100 if base_24h else 0.0

    # open positions + contribution (realized + unrealized per symbol)
    contrib = {}
    for k2 in set(list(lots) + list(realized)):
        dq = lots.get(k2, ())
        amt = sum(l[0] for l in dq)
        cost = sum(l[0] * l[1] for l in dq)
        cur = amt * live_px.get(k2, last_px.get(k2, 0))
        unreal = cur - cost
        total_pnl = realized.get(k2, 0.0) + unreal
        sym = meta.get(k2, {}).get("symbol", k2[1][:6])
        c = contrib.setdefault(sym, {"symbol": sym, "chain": k2[0], "value": 0.0,
                                     "pnl": 0.0, "open": False})
        c["value"] += cur
        c["pnl"] += total_pnl
        c["open"] = c["open"] or amt > 1e-12
    out["positions"] = sorted((c for c in contrib.values() if c["open"]),
                              key=lambda c: -c["value"])
    out["contrib"] = sorted(contrib.values(), key=lambda c: -abs(c["pnl"]))
    out["cash"] = cash
    return out


def simulate_all(cfg):
    """One sim per configured watchlist trader (config 'traders')."""
    out = []
    for key in cfg.get("traders", {}):
        try:
            s = simulate(cfg, key)
            s["handle"] = cfg["traders"][key].get("handle", key)
            out.append(s)
        except Exception as e:
            print(f"  [sim warn] {key}: {str(e)[:120]}")
    return out
