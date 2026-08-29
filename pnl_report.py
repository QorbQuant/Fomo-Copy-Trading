"""Report on the paper-traded copy vault: per-chain coverage, latency, and
hypothetical PnL. Reads data/copy_trades.jsonl (both watchers write there,
tagged with chain). FIFO accounting per (chain, asset) at copy fill prices;
open positions marked at current dexscreener price.

    python pnl_report.py
"""

from collections import defaultdict, deque
from datetime import datetime, timezone

import lib


def chain_of(r):
    return r.get("chain", "robinhood")


def fifo_pnl(copies):
    """-> (realized {key: usd}, positions {key: deque[[amount, price]]})"""
    positions = defaultdict(deque)
    realized = defaultdict(float)
    for r in sorted(copies, key=lambda r: r["block_time"]):
        if not r.get("fill_price_usd") or not r.get("copy_amount"):
            continue
        key = (chain_of(r), r.get("dex_chain", "robinhood"), r["asset_address"], r["asset_symbol"])
        if r["side"] == "buy":
            positions[key].append([r["copy_amount"], r["fill_price_usd"]])
        else:
            qty = r["copy_amount"]
            lots = positions[key]
            while qty > 1e-12 and lots:
                lot = lots[0]
                take = min(qty, lot[0])
                realized[key] += take * (r["fill_price_usd"] - lot[1])
                lot[0] -= take
                qty -= take
                if lot[0] <= 1e-12:
                    lots.popleft()
            # leftover qty = trader sold what the vault never bought (pre-window entry)
    return realized, positions


def main():
    cfg = lib.load_config()
    d = lib.data_dir(cfg)
    records = lib.read_jsonl(d / "copy_trades.jsonl")
    if not records:
        print("No copy records yet. Run watcher.py / solana_watcher.py first.")
        return

    copies = [r for r in records if r["action"] == "copy"]
    realized, positions = fifo_pnl(copies)

    chains = sorted({chain_of(r) for r in records})
    print(f"=== fomo copy vault (paper) — @{cfg['trader']['handle']} ===")
    t0 = min(r["block_time"] for r in copies)
    t1 = max(r["block_time"] for r in copies)
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M")
    print(f"window: {fmt(t0)} .. {fmt(t1)} UTC   vault AUM ${cfg['vault']['aum_usd']:,}\n")

    grand_net = 0.0
    for chain in chains:
        c_all = [r for r in records if chain_of(r) == chain]
        c_cop = [r for r in c_all if r["action"] == "copy"]
        c_skip = len(c_all) - len(c_cop)
        trader_vol = sum(r.get("trader_usd") or 0 for r in c_all)
        copy_vol = sum(r.get("copy_usd") or 0 for r in c_cop)

        lats = sorted(r["latency_s"] for r in c_cop
                      if not r["backfill"] and r.get("latency_s") is not None)
        lat_txt = ""
        if lats:
            med = lats[len(lats) // 2]
            p90 = lats[min(len(lats) - 1, int(0.9 * len(lats)))]
            lat_txt = f"   latency median {med:.1f}s p90 {p90:.1f}s (n={len(lats)})"

        c_real = sum(v for k, v in realized.items() if k[0] == chain)
        c_unreal = 0.0
        open_lines = []
        for key, lots in positions.items():
            if key[0] != chain:
                continue
            amount = sum(l[0] for l in lots)
            if amount <= 1e-12:
                continue
            cost = sum(l[0] * l[1] for l in lots)
            mark = lib.price_usd(key[2], key[1])
            if mark is None:
                open_lines.append(f"    {key[3]:12} {amount:,.4g} @ cost ${cost:,.2f} (no price)")
                continue
            u = amount * mark - cost
            c_unreal += u
            open_lines.append(
                f"    {key[3]:12} {amount:,.4g} cost ${cost:,.2f} now ${amount * mark:,.2f} ({u:+,.2f})")

        net = c_real + c_unreal
        grand_net += net
        print(f"--- {chain}: {len(c_cop)} copied / {c_skip} skipped, "
              f"trader vol ${trader_vol:,.0f}, copied vol ${copy_vol:,.0f}{lat_txt}")
        for key, v in sorted(realized.items(), key=lambda kv: kv[1]):
            if key[0] == chain and abs(v) >= 0.01:
                print(f"    realized {key[3]:12} {v:+10.2f} USD")
        for line in open_lines:
            print(line)
        print(f"    net {chain}: {net:+,.2f} USD  (realized {c_real:+,.2f} / unrealized {c_unreal:+,.2f})\n")

    aum = cfg["vault"]["aum_usd"]
    print(f"net paper PnL all chains: {grand_net:+,.2f} USD on ${aum:,} AUM ({grand_net / aum * 100:+.2f}%)")
    if any(r["backfill"] for r in copies):
        print("note: backfilled trades fill at the trader's implied price (no latency cost); "
              "live-watched trades fill at detection-time prices.")


if __name__ == "__main__":
    main()
