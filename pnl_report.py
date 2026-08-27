"""Report on the paper-traded copy vault: coverage, latency, and hypothetical PnL.

Reads data/copy_trades.jsonl. FIFO position accounting per asset at the copy
fill prices; open positions marked at current dexscreener price.

    python pnl_report.py
"""

from collections import defaultdict, deque
from datetime import datetime, timezone

import lib


def main():
    cfg = lib.load_config()
    d = lib.data_dir(cfg)
    records = lib.read_jsonl(d / "copy_trades.jsonl")
    copies = [r for r in records if r["action"] == "copy"]
    skips = [r for r in records if r["action"] == "skip"]

    if not records:
        print("No copy records yet. Run watcher.py first.")
        return

    live = [r for r in copies if not r["backfill"]]
    lats = sorted(r["latency_s"] for r in live if r.get("latency_s") is not None)
    drifts = [r["latency_drift_bps"] for r in live if r.get("latency_drift_bps") is not None]

    print(f"=== fomo copy vault (paper) — @{cfg['trader']['handle']} on {cfg['chain']['name']} ===")
    print(f"records: {len(copies)} copied, {len(skips)} skipped "
          f"({sum(1 for s in skips if s['reason'] == 'no_usd_value')} unpriced)")
    if copies:
        t0 = min(r["block_time"] for r in copies)
        t1 = max(r["block_time"] for r in copies)
        fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M")
        print(f"window:  {fmt(t0)} .. {fmt(t1)} UTC")
    if lats:
        pct = lambda p: lats[min(len(lats) - 1, int(p * len(lats)))]
        print(f"latency: median {pct(0.5):.1f}s  p90 {pct(0.9):.1f}s  (n={len(lats)} live)")
    if drifts:
        print(f"latency drift: avg {sum(drifts)/len(drifts):+.1f} bps against the vault (n={len(drifts)})")

    # FIFO per asset
    positions = defaultdict(deque)  # asset -> deque of [amount, fill_price]
    realized = defaultdict(float)
    for r in sorted(copies, key=lambda r: r["block_time"]):
        if not r.get("fill_price_usd") or not r.get("copy_amount"):
            continue
        key = (r["asset_address"], r["asset_symbol"])
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
            # qty left over = trader sold what the vault never bought (pre-window entry)

    print("\n--- realized PnL (closed round-trips) ---")
    total_real = 0.0
    for (addr, sym), pnl in sorted(realized.items(), key=lambda kv: kv[1]):
        if abs(pnl) < 0.01:
            continue
        total_real += pnl
        print(f"  {sym:12} {pnl:+10.2f} USD")
    print(f"  {'TOTAL':12} {total_real:+10.2f} USD")

    print("\n--- open positions (marked at current price) ---")
    total_unreal = 0.0
    for (addr, sym), lots in positions.items():
        amount = sum(l[0] for l in lots)
        if amount <= 1e-12:
            continue
        cost = sum(l[0] * l[1] for l in lots)
        mark = lib.price_usd(addr, cfg["chain"]["dexscreener_chain_id"])
        if mark is None:
            print(f"  {sym:12} {amount:,.4g} @ cost ${cost:,.2f} (no current price)")
            continue
        unreal = amount * mark - cost
        total_unreal += unreal
        print(f"  {sym:12} {amount:,.4g} cost ${cost:,.2f} now ${amount * mark:,.2f} ({unreal:+,.2f})")
    print(f"  {'TOTAL':12} {total_unreal:+10.2f} USD unrealized")

    aum = cfg["vault"]["aum_usd"]
    print(f"\nnet paper PnL: {total_real + total_unreal:+,.2f} USD "
          f"on ${aum:,} AUM ({(total_real + total_unreal) / aum * 100:+.2f}%)")
    if any(r["backfill"] for r in copies):
        print("note: backfilled trades use the trader's implied price as the fill "
              "(no latency cost modeled); live-watched trades use detection-time prices.")


if __name__ == "__main__":
    main()
