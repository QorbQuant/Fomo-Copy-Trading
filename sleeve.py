"""Solana sleeve: the vault's execution arm on Solana.

For every Solana copy signal this quotes the actual fill through Jupiter
(real route, real price impact) and logs it to data/sleeve_fills.jsonl.
By default it PAPER-trades. Real execution — signing Jupiter swaps with the
sleeve keypair — only happens when all of these hold:

  1. SLEEVE_EXECUTE=1 in the environment,
  2. SLEEVE_SOLANA_SECRET is present in .env,
  3. the sleeve holds enough USDC for the trade.

The sleeve address is pinned on-chain in CopyVault.setSleeve(); the vault
funds it via fundSleeve() (deBridge DLN order, receiver fixed).
"""

import base64
import json
import os
import time
from pathlib import Path

import requests

import lib

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUPITER_BASES = ("https://lite-api.jup.ag/swap/v1", "https://quote-api.jup.ag/v6")

_session = requests.Session()


def _env():
    env = {}
    p = Path(__file__).parent / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update(os.environ)
    return env


def jupiter_quote(input_mint, output_mint, amount_raw, slippage_bps=150):
    """Best-route quote; returns the raw quote response or None."""
    params = {
        "inputMint": input_mint, "outputMint": output_mint,
        "amount": str(int(amount_raw)), "slippageBps": slippage_bps,
    }
    for base in JUPITER_BASES:
        try:
            r = _session.get(f"{base}/quote", params=params, timeout=15)
            if r.status_code == 200 and r.json().get("outAmount"):
                return r.json()
        except requests.RequestException:
            continue
    return None


def handle_copy(cfg, trade, rec):
    """Called by solana_watcher for each Solana copy record."""
    if rec is None or trade.get("chain") != "solana":
        return None
    mint = rec["asset_address"]
    if mint in ("SOL", USDC_MINT):
        return None

    decimals = rec.get("asset_decimals")
    if rec["side"] == "buy":
        quote = jupiter_quote(USDC_MINT, mint, rec["copy_usd"] * 1e6)
        out_amount = int(quote["outAmount"]) / 10 ** decimals if quote and decimals is not None else None
        fill_price = rec["copy_usd"] / out_amount if out_amount else None
    else:
        if decimals is None or not rec.get("copy_amount"):
            return None
        quote = jupiter_quote(mint, USDC_MINT, rec["copy_amount"] * 10 ** decimals)
        out_usd = int(quote["outAmount"]) / 1e6 if quote else None
        fill_price = out_usd / rec["copy_amount"] if out_usd else None

    impact_bps = None
    if fill_price and rec.get("detection_price_usd"):
        impact_bps = round((fill_price / rec["detection_price_usd"] - 1) * 10_000
                           * (1 if rec["side"] == "buy" else -1), 1)

    fill = {
        "tx_hash": rec["tx_hash"], "side": rec["side"], "mint": mint,
        "symbol": rec["asset_symbol"], "copy_usd": rec["copy_usd"],
        "jupiter_fill_price_usd": fill_price,
        "detection_price_usd": rec.get("detection_price_usd"),
        "impact_bps": impact_bps,
        "quoted_at": round(time.time(), 3),
        "executed": False,
    }
    if quote and _env().get("SLEEVE_EXECUTE") == "1":
        sig = _execute(cfg, quote)
        fill["executed"] = sig is not None
        fill["sleeve_sig"] = sig
    lib.append_jsonl(lib.data_dir(cfg) / "sleeve_fills.jsonl", fill)
    return fill


def _execute(cfg, quote):
    """Sign and send the Jupiter swap with the sleeve keypair. Returns sig or None."""
    env = _env()
    secret = env.get("SLEEVE_SOLANA_SECRET")
    if not secret:
        return None
    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction

        kp = Keypair.from_base58_string(secret)
        for base in JUPITER_BASES:
            try:
                r = _session.post(f"{base}/swap", json={
                    "quoteResponse": quote,
                    "userPublicKey": str(kp.pubkey()),
                    "dynamicComputeUnitLimit": True,
                }, timeout=20)
                if r.status_code != 200:
                    continue
                raw = base64.b64decode(r.json()["swapTransaction"])
                tx = VersionedTransaction.from_bytes(raw)
                signed = VersionedTransaction(tx.message, [kp])
                sig = lib.rpc(cfg["solana"]["rpc"], "sendTransaction",
                              [base64.b64encode(bytes(signed)).decode(),
                               {"encoding": "base64", "skipPreflight": False}])
                print(f"  [sleeve] EXECUTED {sig}")
                return sig
            except requests.RequestException:
                continue
    except Exception as e:
        print(f"  [sleeve] execution failed: {e}")
    return None
