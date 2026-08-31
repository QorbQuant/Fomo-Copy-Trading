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

import json
import os
import sys
import time

import requests

import lib
import sleeve as sleeve_mod
from executor import DEPLOY, Executor, cast_call, cast_send, uint
from watcher import is_funding_token

MIN_WEIGHT = 0.005  # ignore positions under 0.5% of the trader's book
MIN_CLIP_USD = 2.0
CLIP_FRACTION = 0.049  # per-trade, matches the vault's 5% cap with margin

# Anti-poisoning: anyone can airdrop clone tokens (fake PONS/CASHBIRD with
# fake pricing) into the trader's public wallet, inflating computed weights so
# the vault sells real assets to buy scams. A position only counts in the book
# if its deepest pool is real-money deep and could plausibly pay the position
# out.
MIN_BOOK_LIQUIDITY = 25_000  # deepest-pair liquidity, USD
MAX_POSITION_VS_LIQUIDITY = 1.0  # holding "worth" more than its pool is fake


def _robinhood_token_candidates(cfg, blockscout_items):
    """Union of every Robinhood token AJC might hold: Blockscout's list (flaky +
    paginated, best-effort) PLUS every token we've onboarded or seen him trade.
    The latter guarantees a real position (e.g. AI) is never missed just because
    Blockscout dropped it from a page."""
    cands = {}  # addr -> decimals(optional)
    for item in blockscout_items:
        t = item["token"]
        a = (t.get("address") or t.get("address_hash") or "").lower()
        if a:
            cands[a] = int(t.get("decimals") or 18)
    d = lib.data_dir(cfg)
    # tokens the executor has onboarded (routes prove they're tradeable)
    try:
        st = json.loads((d / "executor_state_mainnet.json").read_text())
        for a, tok in st.get("token_map", {}).items():
            if isinstance(tok, dict):
                cands.setdefault(a.lower(), tok.get("decimals", 18))
    except (OSError, ValueError):
        pass
    # tokens seen in recent Robinhood trade detections
    for tr in lib.read_jsonl(d / "trades.jsonl")[-600:]:
        if tr.get("chain") == "robinhood" and tr.get("kind") == "swap":
            a = tr["asset_token"]["address"].lower()
            cands.setdefault(a, tr["asset_token"].get("decimals", 18))
    return cands


def _erc20_balance(rpc_url, token, holder, decimals):
    try:
        out = lib.rpc(rpc_url, "eth_call",
                      [{"to": token, "data": "0x70a08231" + holder.lower().replace("0x", "").rjust(64, "0")},
                       "latest"], retries=2)
        return int(out, 16) / 10 ** decimals
    except Exception:
        return 0.0


def fetch_trader_portfolio(cfg):
    """-> (positions [{addr, symbol, usd, price}], cash_usd, total_usd, excluded)

    Robinhood holdings are read via on-chain balanceOf over a candidate set
    (Blockscout list ∪ onboarded ∪ recently-traded), so a real position is
    never dropped by Blockscout flakiness/pagination."""
    addr = cfg["trader"]["evm_address"]
    rpc = cfg["chain"]["rpc"]
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (copy-vault-sync)"
    items = []
    for _ in range(3):
        try:
            r = s.get(f"https://robinhoodchain.blockscout.com/api/v2/addresses/{addr}/tokens?type=ERC-20",
                      timeout=30)
            if r.status_code == 200 and r.text.lstrip().startswith("{"):
                items = r.json().get("items", [])
                break
        except requests.RequestException:
            pass
        time.sleep(2)

    positions, cash, excluded = [], 0.0, []
    for taddr, dec in _robinhood_token_candidates(cfg, items).items():
        bal = _erc20_balance(rpc, taddr, addr, dec)
        if bal <= 0:
            continue
        info = lib.token_price_info(taddr, cfg["chain"]["dexscreener_chain_id"])
        if not info["price"]:
            continue
        usd = bal * info["price"]
        symbol = info["symbol"] or taddr[:8]
        leg = {"symbol": symbol, "price_usd": info["price"], "liquidity_usd": info["liquidity"]}
        if is_funding_token(leg):
            cash += usd
            continue
        hp = lib.honeypot_reason(info)
        if (info["liquidity"] < MIN_BOOK_LIQUIDITY
                or usd > info["liquidity"] * MAX_POSITION_VS_LIQUIDITY or hp):
            excluded.append({"addr": taddr, "symbol": symbol, "usd": usd,
                             "liquidity": info["liquidity"], "reason": hp or "thin/oversized"})
            continue
        positions.append({"addr": taddr, "symbol": symbol, "usd": usd, "price": info["price"],
                          "chain": "robinhood"})

    # Solana side of the book: same anti-poisoning filters
    sol_addr = cfg["trader"]["solana_address"]
    for program in sleeve_mod.TOKEN_PROGRAMS:
        res = lib.rpc(cfg["solana"]["rpc"], "getTokenAccountsByOwner",
                      [sol_addr, {"programId": program}, {"encoding": "jsonParsed"}])
        for v in (res or {}).get("value", []):
            tinfo = v["account"]["data"]["parsed"]["info"]
            amt = float(tinfo["tokenAmount"]["uiAmount"] or 0)
            if amt == 0:
                continue
            mint = tinfo["mint"]
            if mint == sleeve_mod.USDC_MINT:
                cash += amt
                continue
            info = lib.token_price_info(mint, "solana")
            if not info["price"]:
                continue
            usd = amt * info["price"]
            symbol = info["symbol"] or mint[:6]
            hp = lib.honeypot_reason(info)
            if (info["liquidity"] < MIN_BOOK_LIQUIDITY
                    or usd > info["liquidity"] * MAX_POSITION_VS_LIQUIDITY or hp):
                excluded.append({"addr": mint, "symbol": symbol, "usd": usd,
                                 "liquidity": info["liquidity"], "reason": hp or "thin/oversized"})
                continue
            positions.append({"addr": mint, "symbol": symbol, "usd": usd,
                              "price": info["price"], "chain": "solana",
                              "decimals": tinfo["tokenAmount"]["decimals"]})
    cash += sleeve_mod.sol_balance(cfg, sol_addr) * (
        lib.price_usd("So11111111111111111111111111111111111111112", "solana") or 0)

    total = cash + sum(p["usd"] for p in positions)
    return positions, cash, total, excluded


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

    positions, cash, total, excluded = fetch_trader_portfolio(cfg)
    positions.sort(key=lambda p: -p["usd"])
    nav = ex.compute_nav_usd()
    print(f"trader book ${total:,.0f} ({len(positions)} priced positions, "
          f"${cash:,.0f} cash) -> vault NAV ${nav:,.2f}")
    for e in sorted(excluded, key=lambda e: -e["usd"]):
        if e["usd"] > 100:
            print(f"  [excluded] {e['symbol']:12} claims ${e['usd']:,.0f} but pool depth is "
                  f"${e['liquidity']:,.0f} — airdrop/clone, not counted")

    sleeve_pub = sleeve_mod._env().get("SLEEVE_SOLANA_PUBKEY")

    def current_usd(p):
        if p.get("chain") == "solana":
            held, _ = sleeve_mod.token_balance(cfg, sleeve_pub, p["addr"]) if sleeve_pub else (0, None)
            return held * p["price"]
        return vault_position_usd(ex, p["addr"], p["price"])

    plan = []
    for p in positions:
        weight = p["usd"] / total
        if weight < MIN_WEIGHT:
            continue
        target = nav * weight
        delta = target - current_usd(p)
        if abs(delta) >= MIN_CLIP_USD:
            plan.append({**p, "weight": weight, "target": target, "delta": delta})

    # orphan exits: vault holdings the trader has (effectively) fully exited
    planned = {p["addr"] for p in plan}
    weights = {p["addr"]: p["usd"] / total for p in positions}
    for addr, tok in list(ex.state["token_map"].items()):
        if addr in planned or weights.get(addr, 0) >= MIN_WEIGHT:
            continue
        info = lib.token_price_info(addr, cfg["chain"]["dexscreener_chain_id"])
        if not info["price"]:
            continue
        held = vault_position_usd(ex, addr, info["price"])
        if held >= MIN_CLIP_USD:
            plan.append({"addr": addr, "symbol": info["symbol"] or addr[:8], "usd": 0,
                         "price": info["price"], "weight": weights.get(addr, 0),
                         "target": 0.0, "delta": -held, "chain": "robinhood"})
    # sleeve orphans
    if sleeve_pub:
        for program in sleeve_mod.TOKEN_PROGRAMS:
            res = lib.rpc(cfg["solana"]["rpc"], "getTokenAccountsByOwner",
                          [sleeve_pub, {"programId": program}, {"encoding": "jsonParsed"}])
            for v in (res or {}).get("value", []):
                tinfo = v["account"]["data"]["parsed"]["info"]
                amt = float(tinfo["tokenAmount"]["uiAmount"] or 0)
                mint = tinfo["mint"]
                if amt == 0 or mint == sleeve_mod.USDC_MINT or mint in planned \
                        or weights.get(mint, 0) >= MIN_WEIGHT:
                    continue
                info = lib.token_price_info(mint, "solana")
                held = amt * (info["price"] or 0)
                if held >= MIN_CLIP_USD:
                    plan.append({"addr": mint, "symbol": info["symbol"] or mint[:6], "usd": 0,
                                 "price": info["price"], "weight": 0.0, "target": 0.0,
                                 "delta": -held, "chain": "solana",
                                 "decimals": tinfo["tokenAmount"]["decimals"]})

    for p in plan:
        print(f"  {p['symbol']:12} [{p.get('chain', 'robinhood'):9}] weight {p['weight']*100:5.1f}%  "
              f"target ${p['target']:8,.2f}  "
              f"{'BUY' if p['delta'] > 0 else 'SELL'} ${abs(p['delta']):,.2f}"
              + ("  (trader exited)" if p["target"] == 0 else ""))
    if dry:
        return

    # ---------------- Solana legs: bridge if short, then rebalance the sleeve
    sol_plan = [p for p in plan if p.get("chain") == "solana"]
    if sol_plan and ex.mainnet:
        os.environ["SLEEVE_EXECUTE"] = "1"  # user invoked sync = consent
        buy_need = sum(p["delta"] for p in sol_plan if p["delta"] > 0)
        sell_frees = sum(-p["delta"] for p in sol_plan if p["delta"] < 0) * 0.95
        cash, _ = sleeve_mod.token_balance(cfg, sleeve_pub, sleeve_mod.USDC_MINT)
        vault_cash = uint(ex.call(ex.asset_addr(), "balanceOf(address)(uint256)",
                                  ex.dep["vault"])) / 1e6
        shortfall = min(buy_need + 1 - cash - sell_frees, max(vault_cash - 1, 0))
        if shortfall > cfg["sleeve"].get("min_bridge_usd", 3):
            if not cfg["sleeve"].get("auto_bridge"):
                print(f"  [sync] sleeve short ${shortfall:,.2f} but auto_bridge is off "
                      "in config.json — buys will partial-fill")
            elif ex.bridge_pending_usd() > 0:
                print("  [sync] a bridge order is already in flight — buys will partial-fill")
            elif ex.sleeve_configured():
                try:
                    ex.post_nav(force=True)  # fundSleeve requires fresh NAV
                    ex.bridge_to_sleeve(shortfall, cash)
                    print(f"  [sync] waiting for DLN fill...")
                    t0 = time.time()
                    while time.time() - t0 < 300:
                        time.sleep(10)
                        now_cash, _ = sleeve_mod.token_balance(cfg, sleeve_pub, sleeve_mod.USDC_MINT)
                        if now_cash > cash + shortfall * 0.5:
                            print(f"  [sync] sleeve funded: ${now_cash:,.2f} USDC")
                            break
                    else:
                        print("  [sync] DLN fill not seen in 5min — buys will partial-fill; "
                              "escrow is tracked in bridge_pending.json and counted in NAV")
                except RuntimeError as e:
                    print(f"  [sync] bridge failed ({e}) — continuing with partial fills")
            else:
                print(f"  [sync] sleeve needs ${shortfall:,.2f} more USDC but vault sleeve "
                      "is not configured — buys will partial-fill")
        rotation_min = cfg["sleeve"].get("rotation_bridge_min_usd", 200)
        for p in sorted(sol_plan, key=lambda p: p["delta"]):  # sells first, frees cash
            cash, _ = sleeve_mod.token_balance(cfg, sleeve_pub, sleeve_mod.USDC_MINT)
            if (p["delta"] >= rotation_min and p["delta"] > cash
                    and cfg["sleeve"].get("auto_bridge") and ex.sleeve_configured()
                    and ex.bridge_pending_usd() == 0):
                # big rotation buy the sleeve can't cover: one DLN order does
                # bridge + swap (solver-executed), delivered straight to the sleeve
                try:
                    ex.post_nav(force=True)
                    vault_cash = uint(ex.call(ex.asset_addr(), "balanceOf(address)(uint256)",
                                              ex.dep["vault"])) / 1e6
                    ex.bridge_and_buy(p["addr"], p["symbol"],
                                      min(p["delta"], max(vault_cash - 1, 0)))
                    continue
                except RuntimeError as e:
                    print(f"  [sync] rotation order failed ({e}) — falling back to sleeve cash")
            fill = sleeve_mod.rebalance_position(cfg, p["addr"], p["symbol"], p["delta"],
                                                 p["price"], p.get("decimals"))
            ok = fill and fill.get("executed")
            print(f"  [sync] sleeve {'ok' if ok else 'SKIP'} "
                  f"{'buy' if p['delta'] > 0 else 'sell'} ${abs(p['delta']):,.2f} {p['symbol']}"
                  + (f" ({fill.get('skip_reason')})" if fill and not ok else ""))

    # ---------------- capital repatriation: if planned Robinhood buys exceed
    # vault cash and the sleeve holds USDC beyond its buffer, bridge it back
    if ex.mainnet and sleeve_pub and cfg["sleeve"].get("auto_bridge"):
        rh_buys = sum(p["delta"] for p in plan
                      if p.get("chain") != "solana" and p["delta"] > 0)
        vault_cash = uint(ex.call(ex.asset_addr(), "balanceOf(address)(uint256)",
                                  ex.dep["vault"])) / 1e6
        cash_short = rh_buys - (vault_cash - 1)
        s_usdc, _ = sleeve_mod.token_balance(cfg, sleeve_pub, sleeve_mod.USDC_MINT)
        buffer_target = nav * cfg["sleeve"].get("buffer_pct", 5) / 100
        excess = s_usdc - buffer_target - 10  # keep the gas-treasury floor
        amount = min(cash_short, excess)
        if amount >= 20:
            os.environ["SLEEVE_EXECUTE"] = "1"
            try:
                ex.bridge_back(amount)
            except Exception as e:
                print(f"  [sync] bridge-back failed ({str(e)[:120]}) — buys use vault cash only")

    # ---------------- Robinhood Chain legs (sells first: they fund the buys)
    clip_cap = nav * CLIP_FRACTION
    asset = ex.asset_addr()
    cash_out = False
    for p in sorted((p for p in plan if p.get("chain") != "solana"),
                    key=lambda p: p["delta"]):
        if cash_out and p["delta"] > 0:
            continue
        try:
            tok = ex.ensure_token(p["addr"], p["symbol"], p["price"])
        except RuntimeError as e:
            print(f"  [sync] skipping {p['symbol']}: {e}")
            continue
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
                    print(f"  [sync] out of cash at {p['symbol']} — remaining buys skipped")
                    cash_out = True
                    break
                amount_in = int(clip * 1e6)
                if ex.mainnet:
                    min_out = ex.min_out(tok, "buy", amount_in)
                else:
                    min_out = int(clip / p["price"] * 10 ** dec * 0.97)
                try:
                    tx = ex.send(ex.dep["vault"], "mirrorTrade(address,address,uint256,uint256)",
                                 asset, tok["addr"], amount_in, min_out)
                except RuntimeError as e:
                    print(f"  [sync] {p['symbol']} trade reverted — route marked broken, "
                          f"skipping position ({str(e)[-120:]})")
                    ex.mark_broken(p["addr"])
                    break
            else:
                bal = uint(ex.call(tok["addr"], "balanceOf(address)(uint256)", ex.dep["vault"]))
                amount_in = min(int(clip / p["price"] * 10 ** dec), bal)
                if amount_in == 0:
                    break
                if ex.mainnet:
                    min_out = ex.min_out(tok, "sell", amount_in)
                else:
                    min_out = int(clip * 1e6 * 0.97)
                try:
                    tx = ex.send(ex.dep["vault"], "mirrorTrade(address,address,uint256,uint256)",
                                 tok["addr"], asset, amount_in, min_out)
                except RuntimeError as e:
                    print(f"  [sync] {p['symbol']} trade reverted — route marked broken, "
                          f"skipping position ({str(e)[-120:]})")
                    ex.mark_broken(p["addr"])
                    break
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
