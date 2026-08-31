"""Generic EVM satellite executor — mirrors the trader on a non-home chain.

The vault (shares + in-kind redemption) lives on the HOME chain (Robinhood).
Every OTHER EVM chain is a "satellite": the keeper wallet holds USDC there and
swaps it directly through that chain's Uniswap — no vault contract, no
per-chain deploy (there is nothing to protect but the keeper's own funds).

One process per chain: `satellite.py --chain base`. It tails the shared
copy_trades.jsonl for records tagged with its chain, sizes each against the
shared NAV, and swaps USDC<->token via SwapRouter02 (Uniswap V3). Holdings are
valued into NAV by the home executor. Capital reaches the satellite via
deBridge (the vault bridges USDG->keeper USDC); Arc is the CCTP variant.

Live execution requires SLEEVE_EXECUTE=1 and the keeper funded (USDC + gas) on
the chain. Default is dry-run: discover the route, quote it, log what it WOULD
do — same paper-first discipline as every other leg.

    python3 satellite.py --chain base --dry-run
    python3 satellite.py --chain base            # live (needs funding + SLEEVE_EXECUTE)
"""

import json
import os
import sys
import time

import lib
from executor import E, encode_path, run, uint

MAX_TRADE_FRACTION = 0.049
SLIPPAGE = 0.03
MAX_SIGNAL_AGE_S = 600
MIN_TRADE_USD = 1.0
FEE_TIERS = (100, 500, 3000, 10000)


class Satellite:
    def __init__(self, name):
        self.cfg = lib.load_config()
        self.name = name
        sat = self.cfg["satellites"][name]
        self.dex = sat
        self.rpc = sat["rpc"]
        self.usdc = sat["usdc"]
        self.weth = sat["weth"]
        self.slug = sat["dexscreener_chain_id"]
        d = lib.data_dir(self.cfg)
        self.state_file = d / f"satellite_state_{name}.json"
        self.exec_log = d / f"executions_{name}.jsonl"
        self.state = {"offset": 0, "tokens": {}}
        if self.state_file.exists():
            self.state.update(json.loads(self.state_file.read_text()))

    def save(self):
        self.state_file.write_text(json.dumps(self.state, indent=1))

    def call(self, to, sig, *a):
        return run(["cast", "call", to, sig, *[str(x) for x in a], "--rpc-url", self.rpc])

    def send(self, to, sig, *a, value=None):
        cmd = ["cast", "send", to, sig, *[str(x) for x in a],
               "--rpc-url", self.rpc, "--private-key", E["PRIVATE_KEY"], "--json"]
        if value:
            cmd += ["--value", value]
        for attempt in range(3):
            try:
                out = json.loads(run(cmd))
                break
            except RuntimeError as e:
                if "gas required exceeds allowance" in str(e):
                    raise RuntimeError(f"KEEPER OUT OF GAS on {self.name} — fund {E['DEPLOYER']}") from None
                if "estimate gas" in str(e) and attempt < 2:
                    time.sleep(4)
                    continue
                raise
        if out.get("status") not in ("0x1", 1, "1"):
            raise RuntimeError(f"tx reverted: {out.get('transactionHash')}")
        return out["transactionHash"]

    # ---------------------------------------------------------- route discovery

    def _deepest_fee(self, token, quote, qprice, qdec):
        best = None
        for fee in FEE_TIERS:
            pool = self.call(self.dex["v3_factory"], "getPool(address,address,uint24)(address)",
                             token, quote, fee).strip()
            if int(pool, 16) == 0:
                continue
            depth = uint(self.call(quote, "balanceOf(address)(uint256)", pool)) / 10**qdec * qprice
            if best is None or depth > best[0]:
                best = (depth, fee)
        return best

    def _usdc_weth_fee(self):
        if "uw_fee" not in self.state:
            eth = lib.price_usd(self.weth.lower(), self.slug) or 0
            best = self._deepest_fee(self.weth, self.usdc, 1.0, 6) or (0, 500)
            self.state["uw_fee"] = best[1]
            self.save()
        return self.state["uw_fee"]

    def discover_route(self, token):
        """Best USDC<->token V3 path -> (path_buy, path_sell, desc, depth) or raise."""
        eth = lib.price_usd(self.weth.lower(), self.slug) or 0
        cands = []
        d = self._deepest_fee(token, self.usdc, 1.0, 6)
        if d and d[0] >= 1000:
            cands.append((d[0], encode_path(self.usdc, d[1], token),
                          encode_path(token, d[1], self.usdc), f"USDC direct fee {d[1]}"))
        w = self._deepest_fee(token, self.weth, eth, 18)
        if w and w[0] >= 1000:
            uw = self._usdc_weth_fee()
            cands.append((w[0], encode_path(self.usdc, uw, self.weth, w[1], token),
                          encode_path(token, w[1], self.weth, uw, self.usdc), f"WETH hop fee {w[1]}"))
        for depth, pb, ps, desc in sorted(cands, key=lambda c: -c[0]):
            try:
                if self.quote(pb, 5 * 10**6) > 0:
                    return pb, ps, desc, depth
            except Exception:
                continue
        raise RuntimeError(f"no routable V3 liquidity for {token} on {self.name}")

    def quote(self, path, amount_in):
        out = self.call(self.dex["quoter_v2"],
                        "quoteExactInput(bytes,uint256)(uint256,uint160[],uint32[],uint256)",
                        path, amount_in)
        return uint(out.splitlines()[0])

    def ensure_token(self, token, symbol):
        token = token.lower()
        if token not in self.state["tokens"]:
            dec = uint(self.call(token, "decimals()(uint8)"))
            pb, ps, desc, depth = self.discover_route(token)
            self.state["tokens"][token] = {"symbol": symbol, "decimals": dec,
                                           "path_buy": pb, "path_sell": ps, "route": desc}
            self.save()
            print(f"  [{self.name}] route {symbol}: {desc}, depth ${depth:,.0f}")
        return self.state["tokens"][token]

    # ---------------------------------------------------------- execution

    def keeper_holdings_usd(self):
        """Sum keeper's satellite token positions in USD (for home NAV)."""
        total = 0.0
        for token, t in self.state["tokens"].items():
            bal = uint(self.call(token, "balanceOf(address)(uint256)", E["DEPLOYER"])) / 10**t["decimals"]
            if bal <= 0:
                continue
            px = lib.price_usd(token, self.slug)
            if px:
                total += bal * px
        usdc = uint(self.call(self.usdc, "balanceOf(address)(uint256)", E["DEPLOYER"])) / 1e6
        return total + usdc

    def execute(self, rec, nav, dry):
        price = rec.get("detection_price_usd") or rec.get("trader_implied_price_usd")
        if not price or not rec.get("trader_usd"):
            return
        tok = self.ensure_token(rec["asset_address"], rec["asset_symbol"])
        frac = min(rec["trader_usd"] / self.cfg["trader"]["ref_capital_usd"], MAX_TRADE_FRACTION)
        usd = nav * frac
        router = self.dex["swap_router02"]
        if rec["side"] == "buy":
            if usd < MIN_TRADE_USD:
                return
            amount_in = int(usd * 1e6)
            min_out = int(self.quote(tok["path_buy"], amount_in) * (1 - SLIPPAGE))
            action = f"buy ${usd:,.2f} {tok['symbol']} (minOut {min_out})"
            if not dry:
                self.send(self.usdc, "approve(address,uint256)", router, amount_in)
                tx = self._exact_input(tok["path_buy"], amount_in, min_out)
        else:
            bal = uint(self.call(rec["asset_address"], "balanceOf(address)(uint256)", E["DEPLOYER"]))
            amount_in = min(bal, int(usd / price * 10**tok["decimals"]))
            if amount_in == 0:
                return
            min_out = int(self.quote(tok["path_sell"], amount_in) * (1 - SLIPPAGE))
            action = f"sell {tok['symbol']} -> ${usd:,.2f} (minOut {min_out})"
            if not dry:
                self.send(rec["asset_address"], "approve(address,uint256)", router, amount_in)
                tx = self._exact_input(tok["path_sell"], amount_in, min_out)
        if dry:
            print(f"  [{self.name} DRY] would {action}")
            return
        rec_out = {"ts": round(time.time(), 3), "chain": self.name, "side": rec["side"],
                   "symbol": tok["symbol"], "usd": round(usd, 2), "tx": tx}
        lib.append_jsonl(self.exec_log, rec_out)
        print(f"  [{self.name}] {action} -> {tx}")

    def _exact_input(self, path, amount_in, min_out):
        # SwapRouter02 exactInput((bytes,address,uint256,uint256))
        return self.send(self.dex["swap_router02"],
                         "exactInput((bytes,address,uint256,uint256))(uint256)",
                         f"({path},{E['DEPLOYER']},{amount_in},{min_out})")

    # ---------------------------------------------------------- loop

    def follow(self, dry):
        src = lib.data_dir(self.cfg) / "copy_trades.jsonl"
        navf = lib.data_dir(self.cfg) / "nav_mainnet.json"
        print(f"Satellite {self.name} ({'DRY-RUN' if dry else 'LIVE'}) following {src}")
        while True:
            try:
                size = src.stat().st_size if src.exists() else 0
                if size > self.state["offset"]:
                    nav = json.loads(navf.read_text())["nav_usd"] if navf.exists() else 0
                    with open(src) as f:
                        f.seek(self.state["offset"])
                        lines = f.readlines()
                        self.state["offset"] = f.tell()
                    self.save()
                    for line in lines:
                        rec = json.loads(line)
                        if (rec.get("action") == "copy" and not rec.get("backfill")
                                and rec.get("chain") == self.name
                                and time.time() - rec.get("detected_at", 0) <= MAX_SIGNAL_AGE_S):
                            try:
                                self.execute(rec, nav, dry)
                            except Exception as e:
                                print(f"  [{self.name} warn] {rec.get('asset_symbol')}: {str(e)[:120]}")
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  [{self.name} warn] {str(e)[:120]}")
            time.sleep(4)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    name = sys.argv[sys.argv.index("--chain") + 1]
    dry = "--dry-run" in sys.argv or os.environ.get("SLEEVE_EXECUTE") != "1"
    sat = Satellite(name)
    if "--test" in sys.argv:
        # synthetic: buy $3 of a given token to validate discovery + quote
        token = sys.argv[sys.argv.index("--test") + 1]
        info = lib.token_price_info(token, sat.slug)
        print(f"test: {info['symbol']} @ ${info['price']}")
        sat.execute({"action": "copy", "side": "buy", "asset_address": token,
                     "asset_symbol": info["symbol"] or token[:6], "trader_usd": 2000,
                     "detection_price_usd": info["price"]}, nav=200, dry=True)
        return
    sat.follow(dry)


if __name__ == "__main__":
    main()
