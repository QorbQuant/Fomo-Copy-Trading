"""Watch the trader's Robinhood Chain address and paper-trade a copy vault.

Detects swaps by polling eth_getLogs for ERC-20 Transfer events touching the
trader address (the account is EIP-7702 delegated, so tx.from is fomo's
relayer, never the trader). Normalized trades go to data/trades.jsonl and the
dry-run copier decision for each goes to data/copy_trades.jsonl.

Usage:
    python watcher.py               # backfill recent blocks, then follow live
    python watcher.py --no-backfill # start from the current block
"""

import json
import sys
import time

import copier
import lib

# One-sided transfers of quote-class tokens are funding/bridging (top-ups,
# Relay settlement legs), not trades to copy.
FUNDING_SYMBOLS = {"WETH", "ETH", "USDC", "USDT", "USDG", "DAI", "SOL", "WSOL"}


def is_funding_token(leg):
    if leg["symbol"].upper() in FUNDING_SYMBOLS:
        return True
    # unnamed stables: deep liquidity and pegged to $1
    return (leg["liquidity_usd"] > 500_000 and leg["price_usd"]
            and 0.98 < leg["price_usd"] < 1.02)


_code_cache = {}


def is_contract(rpc_url, addr):
    if addr not in _code_cache:
        _code_cache[addr] = lib.rpc(rpc_url, "eth_getCode", [addr, "latest"]) not in ("0x", None)
    return _code_cache[addr]


def normalize_tx(cfg, tx_hash, logs, detected_at, backfill):
    """Turn one tx's Transfer logs into a normalized trade dict (or None)."""
    trader = cfg["trader"]["evm_address"].lower()
    rpc_url = cfg["chain"]["rpc"]
    chain_slug = cfg["chain"]["dexscreener_chain_id"]

    ins, outs = {}, {}  # token -> raw amount
    counterparties = {"in": set(), "out": set()}
    for lg in logs:
        token = lg["address"].lower()
        sender = "0x" + lg["topics"][1][-40:]
        recipient = "0x" + lg["topics"][2][-40:]
        amount = int(lg["data"], 16)
        if sender == trader:
            outs[token] = outs.get(token, 0) + amount
            counterparties["out"].add(recipient)
        if recipient == trader:
            ins[token] = ins.get(token, 0) + amount
            counterparties["in"].add(sender)

    def leg(token, raw):
        meta = lib.token_meta(rpc_url, token)
        info = lib.token_price_info(token, chain_slug)
        amount = raw / 10 ** meta["decimals"]
        return {
            "address": token, "symbol": meta["symbol"], "amount": amount,
            "price_usd": info["price"], "liquidity_usd": info["liquidity"],
            "usd": amount * info["price"] if info["price"] else None,
        }

    in_legs = sorted((leg(t, a) for t, a in ins.items()), key=lambda l: l["usd"] or 0, reverse=True)
    out_legs = sorted((leg(t, a) for t, a in outs.items()), key=lambda l: l["usd"] or 0, reverse=True)

    block = int(logs[0]["blockNumber"], 16)
    block_time = lib.block_timestamp(rpc_url, logs[0]["blockNumber"])
    base = {
        "chain": cfg["chain"]["name"], "dex_chain": chain_slug,
        "tx_hash": tx_hash, "block": block, "block_time": block_time,
        "detected_at": round(detected_at, 3), "backfill": backfill,
        "latency_s": None if backfill else round(detected_at - block_time, 3),
    }

    if in_legs and out_legs:
        # Two-sided swap: the deeper-liquidity leg is the quote (WETH/USDC
        # style), the other is the asset being traded.
        big_in, big_out = in_legs[0], out_legs[0]
        if big_in["liquidity_usd"] >= big_out["liquidity_usd"]:
            side, asset, quote = "sell", big_out, big_in
        else:
            side, asset, quote = "buy", big_in, big_out
        usd_value = quote["usd"] or asset["usd"]
        implied = usd_value / asset["amount"] if usd_value and asset["amount"] else None
        return {**base, "kind": "swap", "side": side, "usd_value": usd_value,
                "asset_token": {**asset, "implied_price_usd": implied},
                "quote_token": quote, "one_sided": False}

    legs = in_legs or out_legs
    if not legs:
        return None
    asset = legs[0]
    side = "buy" if in_legs else "sell"
    if is_funding_token(asset):
        return {**base, "kind": "funding", "side": side, "usd_value": asset["usd"],
                "asset_token": asset}
    # One-sided moves: a real fomo fill settles against a router CONTRACT
    # (Relay etc.); a transfer where every counterparty is a plain wallet is a
    # migration / CEX deposit / airdrop — never a trade to mirror. This guards
    # both full-exit mirroring (outbound moves) and airdrop poisoning (inbound).
    parties = counterparties["in" if side == "buy" else "out"]
    if parties and not any(is_contract(rpc_url, p) for p in parties):
        return {**base, "kind": "transfer", "side": side, "usd_value": asset["usd"],
                "asset_token": asset}
    # One-sided fill: fomo's unified balance means the other leg can settle on
    # another chain (Relay). Treat as a trade, flagged.
    return {**base, "kind": "swap", "side": side, "usd_value": asset["usd"],
            "asset_token": {**asset, "implied_price_usd": asset["price_usd"]},
            "quote_token": None, "one_sided": True}


def process_range(cfg, from_block, to_block, backfill=False):
    rpc_url = cfg["chain"]["rpc"]
    trader = cfg["trader"]["evm_address"]
    d = lib.data_dir(cfg)
    chunk = cfg["watcher"]["log_chunk_blocks"]

    n_trades = 0
    for start in range(from_block, to_block + 1, chunk):
        end = min(start + chunk - 1, to_block)
        logs = lib.get_transfer_logs(rpc_url, trader, start, end)
        detected_at = time.time()
        by_tx = {}
        for lg in logs:
            by_tx.setdefault(lg["transactionHash"], []).append(lg)
        for tx_hash in sorted(by_tx, key=lambda h: int(by_tx[h][0]["blockNumber"], 16)):
            trade = normalize_tx(cfg, tx_hash, by_tx[tx_hash], detected_at, backfill)
            if trade is None:
                continue
            lib.append_jsonl(d / "trades.jsonl", trade)
            if trade["kind"] == "swap":
                n_trades += 1
                rec = copier.plan_copy(cfg, trade, d / "copy_trades.jsonl")
                _print_trade(trade, rec)
            else:
                print(f"  [{trade['kind']}] {trade['side']} {trade['asset_token']['symbol']} "
                      f"${(trade['usd_value'] or 0):,.0f} {tx_hash[:10]}")
    return n_trades


def _print_trade(trade, rec):
    a = trade["asset_token"]
    lat = "" if trade["backfill"] else f" lat={trade['latency_s']}s"
    copied = f"copy ${rec['copy_usd']}" if rec else "skip"
    tag = " (one-sided)" if trade.get("one_sided") else ""
    print(f"  [{trade['side']:4}] {a['amount']:,.4g} {a['symbol']} "
          f"(${(trade['usd_value'] or 0):,.0f}){tag}{lat} -> {copied}")


def select_chain(cfg):
    """`--chain <name>` swaps cfg['chain'] for a registry entry so one watcher
    process observes one chain. Default keeps the legacy robinhood config.
    Returns the state-file suffix."""
    if "--chain" in sys.argv:
        name = sys.argv[sys.argv.index("--chain") + 1]
        chain = cfg["chains"][name]
        cfg["chain"] = {**cfg["chain"], **chain}  # inherit blockscout etc, override
        # per-chain watcher tuning falls back to the global block if absent
        for k in ("backfill_blocks", "log_chunk_blocks", "poll_seconds"):
            if k in chain:
                cfg["watcher"][k] = chain[k]
        return f"_{name}"
    return ""


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    cfg = lib.load_config()
    suffix = select_chain(cfg)
    rpc_url = cfg["chain"]["rpc"]
    d = lib.data_dir(cfg)
    state_file = d / f"state{suffix}.json"

    def save(block):
        state_file.write_text(json.dumps({"last_block": block}))

    latest = int(lib.rpc(rpc_url, "eth_blockNumber", []), 16)
    if state_file.exists():
        last = json.loads(state_file.read_text())["last_block"]
    else:
        if "--no-backfill" not in sys.argv:
            start = latest - cfg["watcher"]["backfill_blocks"]
            print(f"Backfilling blocks {start + 1}..{latest} ...")
            process_range(cfg, start + 1, latest, backfill=True)
            print("Backfill done. Following live.")
        last = latest
        save(last)
    print(f"Watching {cfg['trader']['evm_address']} on {cfg['chain']['name']} "
          f"(chain {cfg['chain']['chain_id']}) from block {last}...")
    while True:
        time.sleep(cfg["watcher"]["poll_seconds"])
        try:
            head = int(lib.rpc(rpc_url, "eth_blockNumber", []), 16)
            if head > last:
                process_range(cfg, last + 1, head)
                last = head
                save(last)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"  [warn] poll failed, retrying: {e}")


if __name__ == "__main__":
    main()
