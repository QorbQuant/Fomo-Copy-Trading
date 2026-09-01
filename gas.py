"""Gas self-maintenance across both chains.

The sleeve wallet doubles as the ops treasury: when either tank runs low,
it refills from sleeve USDC —
  - Solana SOL:  Jupiter swap USDC -> native SOL (seconds)
  - keeper ETH:  deBridge order from Solana, native ETH delivered to the
                 keeper address on Robinhood Chain (minutes)

Config block (config.json "gas"): auto_refill, evm_min_eth/evm_target_eth,
sol_min/sol_target, max_refill_usd_per_day. Refills are logged to
data/gas_refills.jsonl and count against the daily cap. Gas is an operating
expense: ETH sent to the keeper leaves NAV, deliberately and visibly.

    python3 gas.py --status
    python3 gas.py --refill
"""

import base64
import json
import sys
import time

import requests

import lib
import sleeve

SOL_MINT = "So11111111111111111111111111111111111111112"
NATIVE_ETH = "0x0000000000000000000000000000000000000000"
EVM_RPC = lib.resolve_rpc("robinhood", "https://rpc.mainnet.chain.robinhood.com")
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
MIN_SLEEVE_USDC_FLOOR = 10.0  # never drain the sleeve below this


def keeper_addr():
    from pathlib import Path
    for line in (Path(__file__).parent / "contracts" / ".env").read_text().splitlines():
        if line.startswith("DEPLOYER="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("DEPLOYER missing from contracts/.env")


def keeper_eth(cfg=None):
    out = lib.rpc(EVM_RPC, "eth_getBalance", [keeper_addr(), "latest"])
    return int(out, 16) / 1e18


def _log(cfg, entry):
    lib.append_jsonl(lib.data_dir(cfg) / "gas_refills.jsonl", {"ts": round(time.time(), 3), **entry})


def _spent_today(cfg):
    cutoff = time.time() - 86400
    return sum(e.get("usd", 0) for e in lib.read_jsonl(lib.data_dir(cfg) / "gas_refills.jsonl")
               if e["ts"] > cutoff)


def status(cfg):
    pub = sleeve._env()["SLEEVE_SOLANA_PUBKEY"]
    sol = sleeve.sol_balance(cfg, pub)
    usdc, _ = sleeve.token_balance(cfg, pub, sleeve.USDC_MINT)
    eth = keeper_eth()
    print(f"keeper ETH (Robinhood): {eth:.5f}")
    print(f"sleeve SOL:             {sol:.5f}")
    print(f"sleeve USDC (treasury): ${usdc:.2f}")
    print(f"refilled last 24h:      ${_spent_today(cfg):,.2f}")
    return eth, sol, usdc


def refill_sol(cfg, gcfg, usdc_avail):
    pub = sleeve._env()["SLEEVE_SOLANA_PUBKEY"]
    bal = sleeve.sol_balance(cfg, pub)
    if bal >= gcfg["sol_min"]:
        return 0.0
    sol_price = lib.price_usd(SOL_MINT, "solana") or 0
    usd = (gcfg["sol_target"] - bal) * sol_price
    usd = min(usd, usdc_avail - MIN_SLEEVE_USDC_FLOOR)
    if usd < 1 or not sol_price:
        print(f"  [gas] SOL low ({bal:.4f}) but treasury can't cover refill")
        return 0.0
    quote = sleeve.jupiter_quote(sleeve.USDC_MINT, SOL_MINT, usd * 1e6)
    if not quote:
        return 0.0
    sig = sleeve._execute(cfg, quote)
    if sig:
        print(f"  [gas] refilled SOL: ${usd:,.2f} USDC -> SOL ({sig[:16]}...)")
        _log(cfg, {"kind": "sol_refill", "usd": round(usd, 2), "sig": sig})
        return usd
    return 0.0


DLN_ORDER_SOL_COST = 0.0175  # deBridge flat fee + order rent, in SOL


def solana_dln_order(cfg, usd, dst_token, recipient, dst_chain_id=4663):
    """Create+sign+send a deBridge order from the sleeve: USDC on Solana ->
    dst_token on dst_chain_id (default Robinhood 4663), delivered to `recipient`.
    dst_token 0x0 = native gas on that chain (BNB/MON/ETH). Returns sig."""
    pub = sleeve._env()["SLEEVE_SOLANA_PUBKEY"]
    # deBridge orders carry a real SOL cost — top up first if short
    if sleeve.sol_balance(cfg, pub) < DLN_ORDER_SOL_COST + 0.005:
        usdc_now, _ = sleeve.token_balance(cfg, pub, sleeve.USDC_MINT)
        refill_sol(cfg, cfg.get("gas", {}), usdc_now)
        if sleeve.sol_balance(cfg, pub) < DLN_ORDER_SOL_COST + 0.003:
            raise RuntimeError("sleeve SOL too low for a deBridge order and refill failed")
    r = requests.get("https://dln.debridge.finance/v1.0/dln/order/create-tx", params={
        "srcChainId": 7565164, "srcChainTokenIn": sleeve.USDC_MINT,
        "srcChainTokenInAmount": int(usd * 1e6),
        "dstChainId": dst_chain_id, "dstChainTokenOut": dst_token,
        "dstChainTokenOutRecipient": recipient,
        "srcChainOrderAuthorityAddress": pub,
        "dstChainOrderAuthorityAddress": keeper_addr(),
        "prependOperatingExpenses": "false",
    }, timeout=30, headers={"User-Agent": "copy-vault"})
    body = r.json()
    txdata = (body.get("tx") or {}).get("data")
    if not txdata:
        raise RuntimeError(f"no tx in deBridge response: {json.dumps(body)[:200]}")
    raw = bytes.fromhex(txdata[2:]) if txdata.startswith("0x") else base64.b64decode(txdata)
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    kp = Keypair.from_base58_string(sleeve._env()["SLEEVE_SOLANA_SECRET"])
    tx = VersionedTransaction.from_bytes(raw)
    msg = tx.message
    last_err = None
    for attempt in range(2):
        # deBridge's baked-in blockhash is stale/foreign by send time —
        # re-stamp with a fresh one from OUR rpc before signing
        fresh = lib.rpc(cfg["solana"]["rpc"], "getLatestBlockhash",
                        [{"commitment": "finalized"}])["value"]["blockhash"]
        if isinstance(msg, MessageV0):
            msg = MessageV0(msg.header, msg.account_keys, Hash.from_string(fresh),
                            msg.instructions, msg.address_table_lookups)
        signed = VersionedTransaction(msg, [kp])
        my_sig = str(signed.signatures[0])
        try:
            # retries=1: NEVER blind-retry a broadcast (idempotency hazard)
            return lib.rpc(cfg["solana"]["rpc"], "sendTransaction",
                           [base64.b64encode(bytes(signed)).decode(),
                            {"encoding": "base64", "skipPreflight": False,
                             "preflightCommitment": "processed"}], retries=1)
        except RuntimeError as e:
            last_err = e
            if "AlreadyProcessed" in str(e):
                # a prior attempt landed — succeed/fail on ITS real status
                ok = sleeve.confirm_sig(cfg, my_sig, timeout=45)
                if ok:
                    return my_sig
                raise RuntimeError(f"order tx landed but FAILED on-chain: {my_sig}")
            if "lockhash" in str(e) and attempt == 0:
                time.sleep(2)
                continue
            raise
    raise last_err


def refill_eth(cfg, gcfg, usdc_avail):
    eth = keeper_eth()
    if eth >= gcfg["evm_min_eth"]:
        return 0.0
    eth_price = lib.price_usd(WETH.lower(), "robinhood") or 0
    usd = max((gcfg["evm_target_eth"] - eth) * eth_price, 20)  # amortize DLN fees
    usd = min(usd, usdc_avail - MIN_SLEEVE_USDC_FLOOR)
    if usd < 15 or not eth_price:
        print(f"  [gas] keeper ETH low ({eth:.5f}) but treasury can't cover refill")
        return 0.0
    try:
        sig = solana_dln_order(cfg, usd, NATIVE_ETH, keeper_addr())
        print(f"  [gas] deBridge order sent: ${usd:,.2f} USDC -> ETH @ keeper ({sig[:16]}...) "
              f"— waiting for fill...")
        t0 = time.time()
        while time.time() - t0 < 300:
            time.sleep(10)
            if keeper_eth() > eth + 0.0005:
                print(f"  [gas] keeper refueled: {keeper_eth():.5f} ETH")
                break
        else:
            print("  [gas] fill not seen in 5min — order remains open, check gas_refills log")
        _log(cfg, {"kind": "eth_refill", "usd": round(usd, 2), "sig": sig})
        return usd
    except Exception as e:
        print(f"  [gas] ETH refill failed: {str(e)[:200]}")
        return 0.0


def _last_satellite_gas_ts(cfg, chain):
    """Timestamp of the most recent native-gas order to `chain` (0 if none)."""
    ts = 0.0
    for e in lib.read_jsonl(lib.data_dir(cfg) / "gas_refills.jsonl"):
        if e.get("kind") == "satellite_gas" and e.get("chain") == chain:
            ts = max(ts, e.get("ts", 0))
    return ts


def refill_satellite_gas(cfg):
    """Keep the keeper's NATIVE gas topped up on every LIVE satellite chain, so a
    cross-chain trade never gets missed waiting for gas. Sourced from the sleeve
    (Solana USDC) via a deBridge order delivering native token (0x0) to the
    keeper — the same rail as the home-chain ETH refill, just a different
    dstChainId. Proactive (not on-demand): gas is ready before the trade lands."""
    gcfg = cfg.get("gas", {})
    if not gcfg.get("auto_refill"):
        return
    keeper = keeper_addr()
    for name, sat in cfg.get("satellites", {}).items():
        if not sat.get("live"):
            continue
        try:
            bal = int(lib.rpc(sat["rpc"], "eth_getBalance", [keeper, "latest"]), 16) / 1e18
            native_px = lib.price_usd(sat["weth"].lower(), sat["dexscreener_chain_id"]) or 0
        except Exception as e:
            print(f"  [gas] {name} gas check failed: {str(e)[:80]}")
            continue
        if not native_px:
            continue
        target = sat.get("gas_target_usd", 2.0)
        if bal * native_px >= target * 0.5:  # still over half a tank
            continue
        # in-flight guard: a deBridge gas order takes ~1-3 min to fill; don't
        # re-fire every maintain cycle (~120s) while one is still landing, or we
        # burn duplicate fees and can exhaust the daily cap (starving home gas).
        if time.time() - _last_satellite_gas_ts(cfg, name) < 900:
            continue
        spent = _spent_today(cfg)
        cap = gcfg.get("max_refill_usd_per_day", 60)
        remaining = cap - spent
        if remaining < 3:  # can't afford the deBridge order floor without breaching the cap
            return
        usd = max(min(target - bal * native_px, remaining), 3)  # small; deBridge order floor
        pub = sleeve._env()["SLEEVE_SOLANA_PUBKEY"]
        usdc, _ = sleeve.token_balance(cfg, pub, sleeve.USDC_MINT)
        if usdc - MIN_SLEEVE_USDC_FLOOR < usd:
            print(f"  [gas] {name} native gas low ({bal:.5f}) but treasury can't cover ${usd:.2f}")
            continue
        try:
            sig = solana_dln_order(cfg, usd, NATIVE_ETH, keeper, dst_chain_id=sat["chain_id"])
            print(f"  [gas] {name} gas refill: ${usd:.2f} USDC -> native @ keeper ({sig[:16]}...)")
            _log(cfg, {"kind": "satellite_gas", "chain": name, "usd": round(usd, 2), "sig": sig})
        except Exception as e:
            print(f"  [gas] {name} gas refill failed (deBridge may not support {name}?): {str(e)[:120]}")


def maintain(cfg):
    """Called by the executor on its NAV cadence. Cheap when tanks are full."""
    gcfg = cfg.get("gas", {})
    if not gcfg.get("auto_refill"):
        return
    spent = _spent_today(cfg)
    cap = gcfg.get("max_refill_usd_per_day", 60)
    if spent >= cap:
        return
    pub = sleeve._env()["SLEEVE_SOLANA_PUBKEY"]
    usdc, _ = sleeve.token_balance(cfg, pub, sleeve.USDC_MINT)
    budget = cap - spent
    spent1 = refill_sol(cfg, gcfg, min(usdc, budget + MIN_SLEEVE_USDC_FLOOR))
    usdc -= spent1
    refill_eth(cfg, gcfg, min(usdc, budget - spent1 + MIN_SLEEVE_USDC_FLOOR))
    # keep live satellites' native gas ready so cross-chain trades aren't missed
    try:
        refill_satellite_gas(cfg)
    except Exception as e:
        print(f"  [gas] satellite gas maintenance failed: {str(e)[:100]}")


def main():
    cfg = lib.load_config()
    if "--refill" in sys.argv:
        import os
        os.environ["SLEEVE_EXECUTE"] = "1"
        gcfg = cfg.get("gas", {})
        pub = sleeve._env()["SLEEVE_SOLANA_PUBKEY"]
        usdc, _ = sleeve.token_balance(cfg, pub, sleeve.USDC_MINT)
        refill_sol(cfg, gcfg, usdc)
        usdc, _ = sleeve.token_balance(cfg, pub, sleeve.USDC_MINT)
        refill_eth(cfg, gcfg, usdc)
    status(cfg)


if __name__ == "__main__":
    main()
