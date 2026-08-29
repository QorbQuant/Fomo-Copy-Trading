"""Initial sync / rebalance: bring the vault's holdings to the trader's
current portfolio weights (Hyperliquid-style), after which the executor
mirrors flows.

Reads the trader's live Robinhood Chain portfolio (Blockscout + dexscreener),
computes weights over priced holdings (stables count as cash), and executes
buys/sells on the testnet vault until vault weights match — in clips no
larger than the vault's per-trade cap, re-pinning the mock router to real
prices before each clip. Idempotent: run again any time to correct drift.

    python sync.py            # execute
    python sync.py --dry-run  # print the plan only
"""

import sys
import time

import requests

import lib
from executor import DEPLOY, Executor, cast_call, cast_send, uint
from watcher import is_funding_token

MIN_WEIGHT = 0.005  # ignore positions under 0.5% of the trader's book
MIN_CLIP_USD = 2.0
CLIP_FRACTION = 0.049  # per-trade, matches the vault's 5% cap with margin


def fetch_trader_portfolio(cfg):
    """-> (positions [{addr, symbol, usd, price}], cash_usd, total_usd)"""
    addr = cfg["trader"]["evm_address"]
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (copy-vault-sync)"
    r = s.get(f"https://robinhoodchain.blockscout.com/api/v2/addresses/{addr}/tokens?type=ERC-20",
              timeout=30)
    r.raise_for_status()
    positions, cash = [], 0.0
    for item in r.json().get("items", []):
        t = item["token"]
        taddr = (t.get("address") or t.get("address_hash") or "").lower()
        try:
            bal = int(item["value"]) / 10 ** int(t.get("decimals") or 18)
        except (TypeError, ValueError):
            continue
        info = lib.token_price_info(taddr, cfg["chain"]["dexscreener_chain_id"])
        if not info["price"]:
            continue
        usd = bal * info["price"]
        leg = {"symbol": t.get("symbol") or taddr[:8], "price_usd": info["price"],
               "liquidity_usd": info["liquidity"]}
        if is_funding_token(leg):
            cash += usd
        else:
            positions.append({"addr": taddr, "symbol": leg["symbol"], "usd": usd,
                              "price": info["price"]})
    total = cash + sum(p["usd"] for p in positions)
    return positions, cash, total


def vault_position_usd(ex, real_addr, price):
    tok = ex.state["token_map"].get(real_addr)
    if not tok:
        return 0.0
    bal = uint(ex.call(tok["addr"], "balanceOf(address)(uint256)",
                       ex.dep["vault"])) / 10 ** tok["decimals"]
    return bal * price


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    dry = "--dry-run" in sys.argv
    ex = Executor(mainnet="--mainnet" in sys.argv)
    cfg = ex.cfg

    positions, cash, total = fetch_trader_portfolio(cfg)
    positions.sort(key=lambda p: -p["usd"])
    nav = ex.compute_nav_usd()
    print(f"trader book ${total:,.0f} ({len(positions)} priced positions, "
          f"${cash:,.0f} cash) -> vault NAV ${nav:,.2f}")

    plan = []
    for p in positions:
        weight = p["usd"] / total
        if weight < MIN_WEIGHT:
            continue
        target = nav * weight
        current = vault_position_usd(ex, p["addr"], p["price"])
        delta = target - current
        if abs(delta) >= MIN_CLIP_USD:
            plan.append({**p, "weight": weight, "target": target, "delta": delta})

    for p in plan:
        print(f"  {p['symbol']:12} weight {p['weight']*100:5.1f}%  target ${p['target']:8,.2f}  "
              f"{'BUY' if p['delta'] > 0 else 'SELL'} ${abs(p['delta']):,.2f}")
    if dry:
        return

    clip_cap = nav * CLIP_FRACTION
    asset = ex.asset_addr()
    for p in plan:
        tok = ex.ensure_token(p["addr"], p["symbol"], p["price"])
        dec = tok["decimals"]
        remaining = abs(p["delta"])
        side = "buy" if p["delta"] > 0 else "sell"
        while remaining >= MIN_CLIP_USD:
            clip = min(remaining, clip_cap)
            ex.post_nav(force=True)
            if side == "buy":
                cash_avail = uint(ex.call(asset, "balanceOf(address)(uint256)",
                                          ex.dep["vault"])) / 1e6
                clip = min(clip, cash_avail - 1)
                if clip < MIN_CLIP_USD:
                    print(f"  [sync] out of cash at {p['symbol']}")
                    return
                amount_in = int(clip * 1e6)
                if ex.mainnet:
                    min_out = int(ex.quote_out(tok["path_buy"], amount_in) * 0.97)
                else:
                    min_out = int(clip / p["price"] * 10 ** dec * 0.97)
                tx = ex.send(ex.dep["vault"], "mirrorTrade(address,address,uint256,uint256)",
                             asset, tok["addr"], amount_in, min_out)
            else:
                amount_in = int(clip / p["price"] * 10 ** dec)
                if ex.mainnet:
                    min_out = int(ex.quote_out(tok["path_sell"], amount_in) * 0.97)
                else:
                    min_out = int(clip * 1e6 * 0.97)
                tx = ex.send(ex.dep["vault"], "mirrorTrade(address,address,uint256,uint256)",
                             tok["addr"], asset, amount_in, min_out)
            remaining -= clip
            lib.append_jsonl(lib.data_dir(cfg) / "executions.jsonl",
                             {"ts": round(time.time(), 3), "kind": "sync", "side": side,
                              "symbol": p["symbol"], "usd": round(clip, 2),
                              "price": p["price"], "vault_tx": tx})
            print(f"  [sync] {side} ${clip:,.2f} {p['symbol']} ({remaining:,.0f} to go) {tx[:14]}")

    ex.post_nav(force=True)
    print("sync complete.")


if __name__ == "__main__":
    main()
