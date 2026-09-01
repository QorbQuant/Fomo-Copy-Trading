"""Watch the trader's Solana wallet and paper-trade copies, same pipeline as
the EVM watcher. Swaps are detected from per-mint token balance deltas (plus
native SOL movement) in each confirmed transaction touching the wallet.

IMPORTANT: fomo's co-signer acts as delegate, so most swap txs never reference
the owner wallet — signatures must be collected per TOKEN ACCOUNT (ATA), not
per wallet. We enumerate ATAs with getTokenAccountsByOwner and watch each,
deduping tx signatures (a swap touches two ATAs).

Writes into the same data/trades.jsonl + data/copy_trades.jsonl with
chain="solana" so pnl_report.py can split PnL per chain.

Usage:
    python solana_watcher.py                 # backfill recent sigs, then follow
    python solana_watcher.py --no-backfill
"""

import json
import sys
import time

import copier
import lib
import sleeve
from watcher import is_funding_token, select_trader

WSOL_MINT = "So11111111111111111111111111111111111111112"
SOL_DUST_LAMPORTS = 1_000_000  # ignore <0.001 SOL native movement (rent noise)
TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
)


def sol_rpc(cfg, method, params):
    time.sleep(cfg["solana"]["rpc_pause"])  # stay polite on the public RPC
    return lib.rpc(cfg["solana"]["rpc"], method, params, retries=5)


def fetch_signatures(cfg, address, until=None, limit=100):
    """Newest-first [{signature, blockTime, err}] for one address."""
    opts = {"limit": limit}
    if until:
        opts["until"] = until
    return sol_rpc(cfg, "getSignaturesForAddress", [address, opts]) or []


def fetch_token_accounts(cfg):
    """All of the trader's token-account (ATA) addresses across both programs."""
    trader = cfg["trader"]["solana_address"]
    accounts = []
    for program in TOKEN_PROGRAMS:
        res = sol_rpc(cfg, "getTokenAccountsByOwner",
                      [trader, {"programId": program}, {"encoding": "jsonParsed"}])
        accounts.extend(v["pubkey"] for v in (res or {}).get("value", []))
    return accounts


def fetch_tx(cfg, signature):
    return sol_rpc(cfg, "getTransaction",
                   [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])


def _token_deltas(tx, trader):
    """mint -> {"raw": delta, "decimals": n} for the trader-owned accounts."""
    meta = tx["meta"]
    deltas = {}

    def collect(balances, sign):
        for b in balances:
            if b.get("owner") != trader:
                continue
            mint = b["mint"]
            amt = int(b["uiTokenAmount"]["amount"])
            d = deltas.setdefault(mint, {"raw": 0, "decimals": b["uiTokenAmount"]["decimals"]})
            d["raw"] += sign * amt

    collect(meta.get("preTokenBalances") or [], -1)
    collect(meta.get("postTokenBalances") or [], +1)

    # native SOL leg (fomo's co-signer pays fees, so any sizeable delta is trade flow)
    keys = tx["transaction"]["message"]["accountKeys"]
    for i, k in enumerate(keys):
        pubkey = k["pubkey"] if isinstance(k, dict) else k
        if pubkey == trader:
            lamports = meta["postBalances"][i] - meta["preBalances"][i]
            if abs(lamports) >= SOL_DUST_LAMPORTS:
                d = deltas.setdefault("SOL", {"raw": 0, "decimals": 9})
                d["raw"] += lamports
            break
    return {m: d for m, d in deltas.items() if d["raw"] != 0}


def normalize_tx(cfg, signature, tx, detected_at, backfill):
    if tx is None or tx.get("meta") is None or tx["meta"].get("err") is not None:
        return None
    trader = cfg["trader"]["solana_address"]
    chain_slug = cfg["solana"]["dexscreener_chain_id"]
    deltas = _token_deltas(tx, trader)
    if not deltas:
        return None

    def leg(mint, d):
        lookup = WSOL_MINT if mint == "SOL" else mint
        info = lib.token_price_info(lookup, chain_slug)
        symbol = "SOL" if mint == "SOL" else (info["symbol"] or mint[:6])
        amount = abs(d["raw"]) / 10 ** d["decimals"]
        return {
            "address": mint, "symbol": symbol, "amount": amount, "decimals": d["decimals"],
            "price_usd": info["price"], "liquidity_usd": info["liquidity"],
            "usd": amount * info["price"] if info["price"] else None,
        }

    in_legs = sorted((leg(m, d) for m, d in deltas.items() if d["raw"] > 0),
                     key=lambda l: l["usd"] or 0, reverse=True)
    out_legs = sorted((leg(m, d) for m, d in deltas.items() if d["raw"] < 0),
                      key=lambda l: l["usd"] or 0, reverse=True)

    block_time = tx.get("blockTime") or 0
    base = {
        "chain": "solana", "dex_chain": chain_slug,
        "trader": cfg.get("_trader_key", cfg["trader"].get("handle", "primary")),
        "tx_hash": signature, "block": tx.get("slot"), "block_time": block_time,
        "detected_at": round(detected_at, 3), "backfill": backfill,
        "latency_s": None if backfill or not block_time else round(detected_at - block_time, 3),
    }

    if in_legs and out_legs:
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
    asset = legs[0]
    side = "buy" if in_legs else "sell"
    if is_funding_token(asset):
        return {**base, "kind": "funding", "side": side, "usd_value": asset["usd"],
                "asset_token": asset}
    return {**base, "kind": "swap", "side": side, "usd_value": asset["usd"],
            "asset_token": {**asset, "implied_price_usd": asset["price_usd"]},
            "quote_token": None, "one_sided": True}


def process_signatures(cfg, sig_infos, backfill=False):
    """sig_infos oldest-first. Returns swap count."""
    d = lib.data_dir(cfg)
    n = 0
    for info in sig_infos:
        if info.get("err") is not None:
            continue
        tx = fetch_tx(cfg, info["signature"])
        trade = normalize_tx(cfg, info["signature"], tx, time.time(), backfill)
        if trade is None:
            continue
        out = cfg.get("_out_suffix", "")
        lib.append_jsonl(d / f"trades{out}.jsonl", trade)
        if trade["kind"] == "swap":
            n += 1
            rec = copier.plan_copy(cfg, trade, d / f"copy_trades{out}.jsonl")
            fill = None
            # SAFETY: only the PRIMARY trader (AJC, no --trader) ever executes on
            # the sleeve. Observation-only profiles (out != "") are paper always —
            # they must never reach handle_copy, which trades real sleeve funds.
            if rec and not backfill and out == "":
                fill = sleeve.handle_copy(cfg, trade, rec)
            a = trade["asset_token"]
            lat = "" if backfill else f" lat={trade['latency_s']}s"
            copied = f"copy ${rec['copy_usd']}" if rec else "skip"
            tag = " (one-sided)" if trade.get("one_sided") else ""
            jup = f" jup_impact={fill['impact_bps']}bps" if fill and fill.get("impact_bps") is not None else ""
            print(f"  [sol {trade['side']:4}] {a['amount']:,.4g} {a['symbol']} "
                  f"(${(trade['usd_value'] or 0):,.0f}){tag}{lat} -> {copied}{jup}")
        else:
            print(f"  [sol funding] {trade['side']} {trade['asset_token']['symbol']} "
                  f"${(trade['usd_value'] or 0):,.0f} {trade['tx_hash'][:10]}")
    return n


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    cfg = lib.load_config()
    trader_suffix = select_trader(cfg)
    sol = cfg["solana"]
    d = lib.data_dir(cfg)
    state_file = d / f"state_solana{trader_suffix}.json"

    state = {"last_sigs": {}, "seen": []}
    if state_file.exists():
        state = json.loads(state_file.read_text())
    seen = set(state["seen"])
    last_sigs = state["last_sigs"]

    def save():
        state_file.write_text(json.dumps(
            {"last_sigs": last_sigs, "seen": list(seen)[-5000:]}))

    wallet = cfg["trader"]["solana_address"]
    accounts = [wallet] + fetch_token_accounts(cfg)
    mode = "EXECUTES on sleeve" if cfg.get("_out_suffix", "") == "" else "PAPER only (observation)"
    print(f"Watching @{cfg['_trader_key']} — {len(accounts) - 1} token accounts + wallet on "
          f"solana [{mode}] -> copy_trades{cfg.get('_out_suffix', '')}.jsonl.")

    if not last_sigs and "--no-backfill" not in sys.argv:
        cutoff = time.time() - sol["backfill_days"] * 86400
        batch = {}
        for i, acct in enumerate(accounts):
            for s in fetch_signatures(cfg, acct, limit=sol["backfill_sigs_per_account"]):
                if s["signature"] not in batch:
                    batch[s["signature"]] = s
            head = fetch_signatures(cfg, acct, limit=1)
            last_sigs[acct] = head[0]["signature"] if head else None
            if (i + 1) % 10 == 0:
                print(f"  scanned {i + 1}/{len(accounts)} accounts, {len(batch)} unique sigs")
        recent = sorted((s for s in batch.values() if (s.get("blockTime") or 0) >= cutoff),
                        key=lambda s: s["blockTime"])
        print(f"Backfilling {len(recent)} sigs from the last {sol['backfill_days']}d "
              f"({len(batch) - len(recent)} older skipped) ...")
        process_signatures(cfg, recent, backfill=True)
        seen.update(s["signature"] for s in batch.values())
        save()
        print("Backfill done. Following live.")
    elif not last_sigs:
        for acct in accounts:
            head = fetch_signatures(cfg, acct, limit=1)
            last_sigs[acct] = head[0]["signature"] if head else None
        save()

    cycles = 0
    while True:
        time.sleep(sol["poll_seconds"])
        cycles += 1
        try:
            if cycles % 120 == 0:  # discover ATAs for newly bought tokens
                for acct in fetch_token_accounts(cfg):
                    if acct not in last_sigs:
                        accounts.append(acct)
                        last_sigs[acct] = None
            batch = {}
            for acct in list(last_sigs):
                new = fetch_signatures(cfg, acct, until=last_sigs[acct],
                                       limit=100 if last_sigs[acct] else 5)
                if new:
                    last_sigs[acct] = new[0]["signature"]
                    for s in new:
                        if s["signature"] not in seen and s["signature"] not in batch:
                            batch[s["signature"]] = s
            if batch:
                fresh = sorted(batch.values(), key=lambda s: s.get("blockTime") or 0)
                process_signatures(cfg, fresh)
                seen.update(batch)
                save()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"  [warn] poll failed, retrying: {e}")


if __name__ == "__main__":
    main()
