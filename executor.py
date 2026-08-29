"""Live executor: turns copy signals into real CopyVault transactions.

Tails data/copy_trades.jsonl for live (non-backfill) Robinhood Chain copy
records and mirrors them on-chain. Two environments:

  testnet (default): mocks — deploys a MockERC20 per new real token, pins the
      MockRouter rate to the real token's detection price.
  mainnet (--mainnet): real venue — allowlists the REAL token, discovers the
      deepest Uniswap V3 route (direct USDG pool vs 2-hop via WETH), sets it
      on the adapter, and derives minOut from QuoterV2 rather than an
      off-chain price feed.

Signing is delegated to `cast`/`forge` with the keeper key from
contracts/.env. Executions land in data/executions[_mainnet].jsonl.

Usage:
    python executor.py                     # testnet, follow live signals
    python executor.py --test             # testnet, inject one synthetic buy
    python executor.py --mainnet          # mainnet, follow live signals
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import lib

ROOT = Path(__file__).parent
DEPLOYMENTS = json.loads((ROOT / "contracts" / "deployments.json").read_text())
MAX_TRADE_FRACTION = 0.049  # stay under the vault's 5% on-chain cap
SLIPPAGE = 0.03
MAX_SIGNAL_AGE_S = 600  # never execute a signal detected more than 10 min ago

# Robinhood Chain mainnet infra (Uniswap official deployment, chain 4663)
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
QUOTER_V2 = "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"
V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
FEE_TIERS = (100, 500, 3000, 10000)


def env():
    out = {}
    for line in (ROOT / "contracts" / ".env").read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


E = env()


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}...: {p.stderr.strip()[:300]}")
    return p.stdout.strip()


def uint(s):
    return int(s.split()[0])


def encode_path(*hops):
    """encode_path(tokenA, fee, tokenB[, fee, tokenC]) -> 0x hex path"""
    out = b""
    for h in hops:
        if isinstance(h, int):
            out += h.to_bytes(3, "big")
        else:
            out += bytes.fromhex(h.replace("0x", ""))
    return "0x" + out.hex()


class Executor:
    def __init__(self, mainnet=False):
        self.cfg = lib.load_config()
        self.mainnet = mainnet
        self.dep = DEPLOYMENTS["robinhood-mainnet" if mainnet else "robinhood-testnet"]
        self.rpc = self.dep["rpc"]
        suffix = "_mainnet" if mainnet else ""
        self.exec_log = lib.data_dir(self.cfg) / f"executions{suffix}.jsonl"
        self.state_file = lib.data_dir(self.cfg) / f"executor_state{suffix}.json"
        self.state = {"offset": 0, "token_map": {}, "last_nav_post": 0}
        if self.state_file.exists():
            self.state.update(json.loads(self.state_file.read_text()))
        # migrate old testnet entries (plain mock-address strings)
        for k, v in list(self.state["token_map"].items()):
            if isinstance(v, str):
                self.state["token_map"][k] = {"addr": v, "decimals": 18}

    def save(self):
        self.state_file.write_text(json.dumps(self.state, indent=1))

    def call(self, to, sig, *args):
        return run(["cast", "call", to, sig, *[str(a) for a in args], "--rpc-url", self.rpc])

    def send(self, to, sig, *args, value=None):
        cmd = ["cast", "send", to, sig, *[str(a) for a in args],
               "--rpc-url", self.rpc, "--private-key", E["PRIVATE_KEY"], "--json"]
        if value:
            cmd += ["--value", value]
        out = json.loads(run(cmd))
        if out.get("status") not in ("0x1", 1, "1"):
            raise RuntimeError(f"tx reverted: {out.get('transactionHash')}")
        return out["transactionHash"]

    # ------------------------------------------------------------ tokens

    def ensure_token(self, real_addr, symbol, price):
        if real_addr in self.state["token_map"]:
            tok = self.state["token_map"][real_addr]
        elif self.mainnet:
            tok = self._onboard_mainnet_token(real_addr, symbol)
        else:
            tok = self._onboard_testnet_token(real_addr, symbol)
        if not self.mainnet:
            # pin the mock DEX to the real detection price: USDC(6d) <-> mock(18d)
            self.send(self.dep["router"], "setRate(address,address,uint256)",
                      self.dep["mUSDC"], tok["addr"], int(1e30 / price))
            self.send(self.dep["router"], "setRate(address,address,uint256)",
                      tok["addr"], self.dep["mUSDC"], int(price * 1e6))
        return tok

    def _onboard_testnet_token(self, real_addr, symbol):
        name = "m" + symbol[:14]
        out = run(["forge", "create", "--root", str(ROOT / "contracts"),
                   "src/mocks/Mocks.sol:MockERC20",
                   "--rpc-url", self.rpc, "--private-key", E["PRIVATE_KEY"],
                   "--broadcast", "--json", "--constructor-args", name, name, "18"])
        mock = json.loads(out)["deployedTo"]
        self.send(mock, "mint(address,uint256)", self.dep["router"], 10**30)
        self.send(self.dep["mUSDC"], "mint(address,uint256)", self.dep["router"], 10**15)
        self.send(self.dep["vault"], "setAllowedToken(address,bool)", mock, "true")
        tok = {"addr": mock, "decimals": 18}
        self.state["token_map"][real_addr] = tok
        self.save()
        print(f"  [exec] deployed {name} -> {mock}")
        return tok

    def _onboard_mainnet_token(self, token, symbol):
        """Allowlist the real token and set the deepest Uniswap V3 route."""
        decimals = uint(self.call(token, "decimals()(uint8)"))
        eth_price = lib.price_usd(WETH.lower(), "robinhood") or 0
        best = None  # (depth_usd, quote, fee)
        for quote, qprice, qdec in ((USDG, 1.0, 6), (WETH, eth_price, 18)):
            for fee in FEE_TIERS:
                pool = self.call(V3_FACTORY, "getPool(address,address,uint24)(address)",
                                 token, quote, fee).strip()
                if int(pool, 16) == 0:
                    continue
                depth = uint(self.call(quote, "balanceOf(address)(uint256)", pool)) / 10**qdec * qprice
                if best is None or depth > best[0]:
                    best = (depth, quote, fee)
        if not best or best[0] < 1000:
            raise RuntimeError(f"no usable pool for {symbol} (best depth ${0 if not best else best[0]:,.0f})")
        _, quote, fee = best
        if quote == USDG:
            path_buy = encode_path(USDG, fee, token)
            path_sell = encode_path(token, fee, USDG)
        else:
            uw_fee = self._usdg_weth_fee()
            path_buy = encode_path(USDG, uw_fee, WETH, fee, token)
            path_sell = encode_path(token, fee, WETH, uw_fee, USDG)
        self.send(self.dep["adapter"], "setPath(address,address,bytes)", USDG, token, path_buy)
        self.send(self.dep["adapter"], "setPath(address,address,bytes)", token, USDG, path_sell)
        self.send(self.dep["vault"], "setAllowedToken(address,bool)", token, "true")
        tok = {"addr": token, "decimals": decimals, "path_buy": path_buy, "path_sell": path_sell,
               "pool_depth_usd": round(best[0])}
        self.state["token_map"][token.lower()] = tok
        self.save()
        print(f"  [exec] onboarded {symbol}: fee {fee} via "
              f"{'USDG direct' if quote == USDG else 'WETH hop'}, depth ${best[0]:,.0f}")
        return tok

    def _usdg_weth_fee(self):
        if "usdg_weth_fee" in self.state:
            return self.state["usdg_weth_fee"]
        best = None
        for fee in FEE_TIERS:
            pool = self.call(V3_FACTORY, "getPool(address,address,uint24)(address)",
                             USDG, WETH, fee).strip()
            if int(pool, 16) == 0:
                continue
            depth = uint(self.call(WETH, "balanceOf(address)(uint256)", pool))
            if best is None or depth > best[0]:
                best = (depth, fee)
        self.state["usdg_weth_fee"] = best[1]
        self.save()
        return best[1]

    def quote_out(self, path, amount_in):
        out = self.call(QUOTER_V2,
                        "quoteExactInput(bytes,uint256)(uint256,uint160[],uint32[],uint256)",
                        path, amount_in)
        return uint(out.splitlines()[0])

    # ------------------------------------------------------------ NAV

    def asset_addr(self):
        return USDG if self.mainnet else self.dep["mUSDC"]

    def compute_nav_usd(self):
        nav = uint(self.call(self.asset_addr(), "balanceOf(address)(uint256)", self.dep["vault"])) / 1e6
        nav += uint(self.call(self.dep["vault"], "sleeveFundedAsset()(uint256)")) / 1e6
        for real_addr, tok in self.state["token_map"].items():
            bal = uint(self.call(tok["addr"], "balanceOf(address)(uint256)",
                                 self.dep["vault"])) / 10 ** tok["decimals"]
            if bal == 0:
                continue
            price = lib.price_usd(real_addr, self.cfg["chain"]["dexscreener_chain_id"])
            if price:
                nav += bal * price
        return nav

    def post_nav(self, force=False):
        if not force and time.time() - self.state["last_nav_post"] < 600:
            return
        nav = self.compute_nav_usd()
        self.send(self.dep["vault"], "postNav(uint256)", int(nav * 1e6))
        self.state["last_nav_post"] = time.time()
        self.save()
        print(f"  [exec] posted NAV ${nav:,.2f}")
        return nav

    # ------------------------------------------------------------ trades

    def execute(self, rec):
        price = rec.get("detection_price_usd") or rec.get("trader_implied_price_usd")
        if not price or not rec.get("trader_usd"):
            return
        tok = self.ensure_token(rec["asset_address"], rec["asset_symbol"], price)
        self.post_nav(force=True)
        nav = self.compute_nav_usd()

        fraction = min(rec["trader_usd"] / self.cfg["trader"]["ref_capital_usd"], MAX_TRADE_FRACTION)
        usd = nav * fraction
        if usd < 1:
            return
        asset = self.asset_addr()

        if rec["side"] == "buy":
            amount_in = int(usd * 1e6)
            if self.mainnet:
                min_out = int(self.quote_out(tok["path_buy"], amount_in) * (1 - SLIPPAGE))
            else:
                min_out = int(usd / price * 10 ** tok["decimals"] * (1 - SLIPPAGE))
            tx = self.send(self.dep["vault"], "mirrorTrade(address,address,uint256,uint256)",
                           asset, tok["addr"], amount_in, min_out)
        else:
            bal = uint(self.call(tok["addr"], "balanceOf(address)(uint256)", self.dep["vault"]))
            amount_in = min(bal, int(usd / price * 10 ** tok["decimals"]))
            if amount_in == 0:
                return
            if self.mainnet:
                min_out = int(self.quote_out(tok["path_sell"], amount_in) * (1 - SLIPPAGE))
            else:
                min_out = int(amount_in / 10 ** tok["decimals"] * price * 1e6 * (1 - SLIPPAGE))
            tx = self.send(self.dep["vault"], "mirrorTrade(address,address,uint256,uint256)",
                           tok["addr"], asset, amount_in, min_out)

        record = {"ts": round(time.time(), 3), "signal_tx": rec["tx_hash"], "side": rec["side"],
                  "symbol": rec["asset_symbol"], "usd": round(usd, 2), "price": price,
                  "vault_tx": tx, "token": tok["addr"], "env": "mainnet" if self.mainnet else "testnet"}
        lib.append_jsonl(self.exec_log, record)
        print(f"  [exec] {rec['side']} ${usd:,.2f} {rec['asset_symbol']} on-chain: {tx}")

    # ------------------------------------------------------------ loop

    def follow(self):
        if not self.dep.get("vault"):
            raise SystemExit("deployments.json has no vault for this environment — deploy first")
        src = lib.data_dir(self.cfg) / "copy_trades.jsonl"
        print(f"Executor ({'MAINNET' if self.mainnet else 'testnet'}) following {src} "
              f"against vault {self.dep['vault']}")
        while True:
            try:
                size = src.stat().st_size if src.exists() else 0
                if size > self.state["offset"]:
                    with open(src) as f:
                        f.seek(self.state["offset"])
                        lines = f.readlines()
                        self.state["offset"] = f.tell()
                    self.save()
                    for line in lines:
                        rec = json.loads(line)
                        if (rec.get("action") == "copy" and not rec.get("backfill")
                                and rec.get("chain", "robinhood") == "robinhood"):
                            if time.time() - rec.get("detected_at", 0) > MAX_SIGNAL_AGE_S:
                                print(f"  [exec] stale signal skipped: {rec['asset_symbol']}")
                                continue
                            self.execute(rec)
                self.post_nav()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  [exec warn] {e}")
            time.sleep(3)


# kept for sync.py imports
def cast_call(to, sig, *args):
    return _default().call(to, sig, *args)


def cast_send(to, sig, *args, value=None):
    return _default().send(to, sig, *args, value=value)


_DEFAULT = None


def _default():
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Executor(mainnet="--mainnet" in sys.argv)
    return _DEFAULT


DEPLOY = DEPLOYMENTS["robinhood-mainnet" if "--mainnet" in sys.argv else "robinhood-testnet"]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    ex = _default()
    if "--test" in sys.argv and not ex.mainnet:
        chill = "0xbbf2c91fdcc488ba736e0c38adc82c9a92597deb"
        price = lib.price_usd(chill, ex.cfg["chain"]["dexscreener_chain_id"])
        info = lib.token_price_info(chill, ex.cfg["chain"]["dexscreener_chain_id"])
        print(f"test signal: buy $2,000 {info['symbol']} @ {price}")
        ex.execute({"action": "copy", "side": "buy", "tx_hash": "TEST",
                    "asset_address": chill, "asset_symbol": info["symbol"] or "TEST",
                    "trader_usd": 2000, "detection_price_usd": price})
    ex.follow()


if __name__ == "__main__":
    main()
