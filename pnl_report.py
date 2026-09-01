"""Report on the paper-traded copy vault: per-chain coverage, latency, and
hypothetical PnL. Reads data/copy_trades.jsonl (both watchers write there,
tagged with chain). FIFO accounting per (chain, asset) at copy fill prices;
open positions marked at current dexscreener price.

    python pnl_report.py                    # primary trader (AJC)
    python pnl_report.py --trader frankdegods   # an observation-only profile
"""

from collections import defaultdict, deque
from datetime import datetime, timezone

import lib
from watcher import select_trader


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
    suffix = select_trader(cfg)  # --trader <key> reads copy_trades_<key>.jsonl
    d = lib.data_dir(cfg)
    records = lib.read_jsonl(d / f"copy_trades{suffix}.jsonl")
    if not records:
        who = f" for @{cfg['_trader_key']}" if suffix else ""
        print(f"No copy records{who} yet. Run watcher.py / solana_watcher.py first.")
        return

    copies = [r for r in records if r["action"] == "copy"]
    if not copies:
        skips = len(records)
        print(f"=== @{cfg['trader']['handle']} (paper): {skips} records, none copyable yet "
              f"(all below the ${cfg['vault']['min_copy_usd']} min or no USD price). "
              f"Nothing to report until a large-enough trade lands. ===")
        return
    realized, positions = fifo_pnl(copies)

    chains = sorted({chain_of(r) for r in records})
    print(f"=== fomo copy vault (paper) — @{cfg['trader']['handle']} ===")
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M")
    # block_time can be 0 for a Solana tx whose RPC omitted blockTime; ignore
    # those so the window doesn't collapse to 1970. Fall back to detected_at.
    btimes = ([r["block_time"] for r in copies if r.get("block_time")]
              or [r["detected_at"] for r in copies if r.get("detected_at")] or [0])
    print(f"window: {fmt(min(btimes))} .. {fmt(max(btimes))} UTC   "
          f"vault AUM ${cfg['vault']['aum_usd']:,}\n")

    grand_net = 0.0
    for chain in chains:
        c_all = [r for r in records if chain_of(r) == chain]
        c_cop = [r for r in c_all if r["action"] == "copy"]
        c_skip = len(c_all) - len(c_cop)
        trader_vol = sum(r.get("trader_usd") or 0 for r in c_all)
        copy_vol = sum(r.get("copy_usd") or 0 for r in c_cop)

        # per-chain window; p10 start dodges backfill stragglers from months back
        times = sorted(r["block_time"] for r in c_all if r.get("block_time"))
        if times:
            w0, w1 = times[len(times) // 10], times[-1]
            days = max((w1 - w0) / 86400, 1 / 24)
            window_txt = f" over {days:.1f}d ({fmt(w0)}..{fmt(w1)}) = ${trader_vol / days:,.0f}/day"
        else:
            window_txt = ""

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
              f"trader vol ${trader_vol:,.0f}{window_txt}, copied vol ${copy_vol:,.0f}{lat_txt}")
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
