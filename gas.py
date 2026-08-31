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
EVM_RPC = "https://rpc.mainnet.chain.robinhood.com"
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
    pub = sleeve._env()["SLEEVE_SOLANA_PUBKEY"]
    try:
        r = requests.get("https://dln.debridge.finance/v1.0/dln/order/create-tx", params={
            "srcChainId": 7565164, "srcChainTokenIn": sleeve.USDC_MINT,
            "srcChainTokenInAmount": int(usd * 1e6),
            "dstChainId": 4663, "dstChainTokenOut": NATIVE_ETH,
            "dstChainTokenOutRecipient": keeper_addr(),
            "srcChainOrderAuthorityAddress": pub,
            "dstChainOrderAuthorityAddress": keeper_addr(),
            "prependOperatingExpenses": "false",
        }, timeout=30, headers={"User-Agent": "copy-vault"})
        body = r.json()
        txdata = (body.get("tx") or {}).get("data")
        if not txdata:
            raise RuntimeError(f"no tx in deBridge response: {json.dumps(body)[:200]}")
        raw = bytes.fromhex(txdata[2:]) if txdata.startswith("0x") else base64.b64decode(txdata)
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        kp = Keypair.from_base58_string(sleeve._env()["SLEEVE_SOLANA_SECRET"])
        tx = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [kp])
        sig = lib.rpc(cfg["solana"]["rpc"], "sendTransaction",
                      [base64.b64encode(bytes(signed)).decode(),
                       {"encoding": "base64", "skipPreflight": False}])
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
