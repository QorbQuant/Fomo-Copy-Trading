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
BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
JUPITER_BASES = ("https://lite-api.jup.ag/swap/v1", "https://quote-api.jup.ag/v6")

_session = requests.Session()


def _sol(cfg, method, params):
    return lib.rpc(cfg["solana"]["rpc"], method, params, retries=5)


TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)


def sol_balance(cfg, pubkey):
    return ((_sol(cfg, "getBalance", [pubkey, {"commitment": "confirmed"}]) or {}).get("value") or 0) / 1e9


def token_balance(cfg, owner, mint):
    """-> (ui_amount, decimals or None)"""
    res = _sol(cfg, "getTokenAccountsByOwner",
               [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    total, dec = 0.0, None
    for v in (res or {}).get("value", []):
        ta = v["account"]["data"]["parsed"]["info"]["tokenAmount"]
        total += float(ta["uiAmount"] or 0)
        dec = ta["decimals"]
    return total, dec


def sleeve_holdings(cfg, pubkey):
    """Itemized sleeve contents: [{symbol, mint, amount, usd}] — SOL (gas), the
    USDC buffer, and every token position, dexscreener-priced."""
    out = []
    sol = sol_balance(cfg, pubkey)
    if sol > 0:
        px = lib.price_usd("So11111111111111111111111111111111111111112", "solana") or 0
        out.append({"symbol": "SOL (gas)", "mint": "SOL", "amount": sol, "usd": sol * px})
    for program in TOKEN_PROGRAMS:
        res = _sol(cfg, "getTokenAccountsByOwner",
                   [pubkey, {"programId": program},
                    {"encoding": "jsonParsed", "commitment": "confirmed"}])
        for v in (res or {}).get("value", []):
            info = v["account"]["data"]["parsed"]["info"]
            amt = float(info["tokenAmount"]["uiAmount"] or 0)
            if amt == 0:
                continue
            mint = info["mint"]
            if mint == USDC_MINT:
                out.append({"symbol": "USDC (buffer)", "mint": mint, "amount": amt, "usd": amt})
                continue
            pinfo = lib.token_price_info(mint, "solana")
            usd = amt * (pinfo["price"] or 0)
            out.append({"symbol": pinfo["symbol"] or mint[:6], "mint": mint,
                        "amount": amt, "usd": usd})
    out.sort(key=lambda h: -h["usd"])
    return out


def sleeve_value_usd(cfg, pubkey):
    """Total sleeve value: USDC + SOL + every token position, dexscreener-priced."""
    return sum(h["usd"] for h in sleeve_holdings(cfg, pubkey))


def _read_vault_nav(cfg, max_age_s=900):
    """Last MAINNET NAV the executor posted; None if stale."""
    p = lib.data_dir(cfg) / "nav_mainnet.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        if time.time() - d["ts"] > max_age_s:
            return None
        return d["nav_usd"]
    except (ValueError, KeyError):
        return None


def mint_decimals(cfg, mint):
    return ((_sol(cfg, "getTokenSupply", [mint]) or {}).get("value") or {}).get("decimals")


def confirm_sig(cfg, sig, timeout=75):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = _sol(cfg, "getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
        v = ((st or {}).get("value") or [None])[0]
        if v:
            return v.get("err") is None
        time.sleep(2)
    return None


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
        # honeypot / spoof guard: skip tokens that look unsellable
        hp = lib.honeypot_reason(lib.token_price_info(mint, cfg["solana"]["dexscreener_chain_id"]))
        if hp:
            lib.append_jsonl(lib.data_dir(cfg) / "sleeve_fills.jsonl",
                             {"tx_hash": rec["tx_hash"], "side": "buy", "mint": mint,
                              "symbol": rec["asset_symbol"], "executed": False,
                              "skip_reason": f"honeypot: {hp}", "quoted_at": round(time.time(), 3)})
            return None
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
        exec_quote, note = quote, None
        if rec["tx_hash"] != "SLEEVE-TEST" and rec["side"] == "sell":
            # mirror the fraction of the position the trader sold; a full
            # exit sells the sleeve's whole position
            pub = _env().get("SLEEVE_SOLANA_PUBKEY")
            held, hdec = token_balance(cfg, pub, mint) if pub else (0, None)
            if held <= 0 or hdec is None:
                fill["executed"] = False
                fill["skip_reason"] = "sleeve holds none"
                lib.append_jsonl(lib.data_dir(cfg) / "sleeve_fills.jsonl", fill)
                return fill
            remaining, _ = token_balance(cfg, cfg["trader"]["solana_address"], mint)
            sold = rec.get("trader_amount") or 0
            frac = sold / (sold + remaining) if sold + remaining > 0 else 0
            if rec.get("one_sided"):
                # can't distinguish a wallet move from a sell on this path:
                # bound the mirror, let sync converge the rest
                frac = min(frac, 0.33)
            elif frac >= 0.95:
                frac = 1.0
            sell_amt = held * frac
            if sell_amt * (rec.get("detection_price_usd") or 0) < 0.25 and frac < 1.0:
                fill["executed"] = False
                fill["skip_reason"] = "sell below min"
                lib.append_jsonl(lib.data_dir(cfg) / "sleeve_fills.jsonl", fill)
                return fill
            note = f"selling {frac*100:.0f}% of sleeve position"
            exec_quote = jupiter_quote(mint, USDC_MINT, sell_amt * 10 ** hdec)
        if rec["tx_hash"] != "SLEEVE-TEST" and rec["side"] == "buy":
            # size against REAL combined NAV, not the paper AUM the copier
            # used; cap at sleeve cash (partial fill beats a failed swap)
            nav = _read_vault_nav(cfg)
            pub = _env().get("SLEEVE_SOLANA_PUBKEY")
            if nav is None or not pub:
                fill["executed"] = False
                fill["skip_reason"] = "no fresh NAV for live sizing"
                lib.append_jsonl(lib.data_dir(cfg) / "sleeve_fills.jsonl", fill)
                return fill
            fraction = min((rec.get("trader_usd") or 0) / cfg["trader"]["ref_capital_usd"], 0.049)
            want = nav * fraction
            cash, _ = token_balance(cfg, pub, USDC_MINT)
            exec_usd = min(want, max(cash - 1.0, 0))
            fill["exec_usd"], fill["exec_want_usd"] = round(exec_usd, 2), round(want, 2)
            if exec_usd < 1:
                fill["executed"] = False
                fill["skip_reason"] = f"sleeve cash ${cash:.2f} below min"
                lib.append_jsonl(lib.data_dir(cfg) / "sleeve_fills.jsonl", fill)
                return fill
            if exec_usd < want:
                note = f"partial fill: wanted ${want:,.2f}, sleeve cash allows ${exec_usd:,.2f}"
            exec_quote = jupiter_quote(USDC_MINT, mint, exec_usd * 1e6)
        if note:
            print(f"  [sleeve] {note}")
            fill["note"] = note
        sig = _execute(cfg, exec_quote) if exec_quote else None
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
                my_sig = str(signed.signatures[0])
                try:
                    sig = lib.rpc(cfg["solana"]["rpc"], "sendTransaction",
                                  [base64.b64encode(bytes(signed)).decode(),
                                   {"encoding": "base64", "skipPreflight": False}],
                                  retries=1)
                except RuntimeError as e:
                    if "AlreadyProcessed" not in str(e):
                        raise
                    sig = my_sig  # a prior attempt landed; confirm decides below
                print(f"  [sleeve] sent {sig} — confirming...")
                ok = confirm_sig(cfg, sig)
                if ok:
                    print(f"  [sleeve] CONFIRMED {sig}")
                    return sig
                print(f"  [sleeve] {'FAILED on-chain' if ok is False else 'confirmation timeout'}: {sig}")
                return None
            except requests.RequestException:
                continue
    except Exception as e:
        print(f"  [sleeve] execution failed: {e}")
    return None


def rebalance_position(cfg, mint, symbol, delta_usd, price, decimals):
    """Sync's convergence primitive: move the sleeve's position in `mint` by
    delta_usd (buy if positive, sell if negative) through Jupiter. Returns a
    fill dict (executed flag included). Caller ensures SLEEVE_EXECUTE consent.
    """
    pub = _env().get("SLEEVE_SOLANA_PUBKEY")
    if not pub:
        return None
    if delta_usd > 0:
        cash, _ = token_balance(cfg, pub, USDC_MINT)
        usd = min(delta_usd, max(cash - 1.0, 0))
        if usd < 1:
            return {"executed": False, "skip_reason": f"sleeve cash ${cash:.2f}"}
        quote = jupiter_quote(USDC_MINT, mint, usd * 1e6)
    else:
        held, hdec = token_balance(cfg, pub, mint)
        if held <= 0:
            return {"executed": False, "skip_reason": "nothing held"}
        amt = min(held, abs(delta_usd) / price if price else held)
        if abs(delta_usd) >= held * (price or 0) * 0.95:
            amt = held  # near-full target reduction: exit cleanly
        quote = jupiter_quote(mint, USDC_MINT, amt * 10 ** (hdec or decimals or 9))
    if not quote:
        return {"executed": False, "skip_reason": "no jupiter route"}
    sig = _execute(cfg, quote)
    fill = {"tx_hash": "SYNC", "side": "buy" if delta_usd > 0 else "sell", "mint": mint,
            "symbol": symbol, "usd": round(abs(delta_usd), 2), "executed": sig is not None,
            "sleeve_sig": sig, "quoted_at": round(time.time(), 3)}
    lib.append_jsonl(lib.data_dir(cfg) / "sleeve_fills.jsonl", fill)
    return fill


def main():
    """Self-test of the REAL execution leg with a synthetic signal — no
    dependency on the tracked trader. Needs the sleeve funded with a little
    SOL (gas) and USDC. Round trip:

        python3 sleeve.py --test-buy 2     # buy $2 of BONK via Jupiter
        python3 sleeve.py --test-sell      # sell the whole BONK balance back
    """
    import json as _json
    import sys
    cfg = lib.load_config()
    pub = _env().get("SLEEVE_SOLANA_PUBKEY")
    if not pub:
        raise SystemExit("SLEEVE_SOLANA_PUBKEY missing from .env")

    def balances():
        sol = sol_balance(cfg, pub)
        usdc, _ = token_balance(cfg, pub, USDC_MINT)
        bonk, bdec = token_balance(cfg, pub, BONK_MINT)
        print(f"  sleeve {pub}\n  SOL {sol:.5f}   USDC {usdc:.2f}   BONK {bonk:,.0f}")
        return sol, usdc, bonk, bdec

    print("before:")
    sol, usdc, bonk, bdec = balances()
    info = lib.token_price_info(BONK_MINT, cfg["solana"]["dexscreener_chain_id"])
    dec = bdec if bdec is not None else mint_decimals(cfg, BONK_MINT)
    os.environ["SLEEVE_EXECUTE"] = "1"

    if "--test-buy" in sys.argv:
        i = sys.argv.index("--test-buy")
        usd = float(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 2.0
        if sol < 0.003:
            raise SystemExit(f"need ~0.003+ SOL for fees, have {sol:.5f} — fund {pub}")
        if usdc < usd:
            raise SystemExit(f"need ${usd} USDC in the sleeve, have ${usdc:.2f} — fund {pub}")
        rec = {"side": "buy", "tx_hash": "SLEEVE-TEST", "asset_address": BONK_MINT,
               "asset_symbol": info["symbol"] or "BONK", "asset_decimals": dec,
               "copy_usd": usd, "detection_price_usd": info["price"]}
    elif "--test-sell" in sys.argv:
        if bonk <= 0:
            raise SystemExit("no BONK to sell — run --test-buy first")
        rec = {"side": "sell", "tx_hash": "SLEEVE-TEST", "asset_address": BONK_MINT,
               "asset_symbol": info["symbol"] or "BONK", "asset_decimals": dec,
               "copy_amount": bonk, "copy_usd": bonk * (info["price"] or 0),
               "detection_price_usd": info["price"]}
    else:
        raise SystemExit(main.__doc__)

    fill = handle_copy(cfg, {"chain": "solana"}, rec)
    print(_json.dumps(fill, indent=1))
    print("after:")
    balances()


if __name__ == "__main__":
    main()
