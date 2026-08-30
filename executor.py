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

import requests

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
V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
POSITION_MANAGER = "0x58daec3116aae6d93017baaea7749052e8a04fa7"
FEE_TIERS = (100, 500, 3000, 10000)
ZERO = "0x0000000000000000000000000000000000000000"

# Route legs (python-side mirror of RouteAdapter.Leg):
#   {"kind": 0, "path": "0x.."}                                    v3 multihop
#   {"kind": 1, "key": {"c0","c1","fee","tick","hooks"}, "zf": b}  v4 single pool
SET_ROUTE_SIG = "setRoute(address,address,(uint8,bytes,(address,address,uint24,int24,address),bool)[])"


def leg_cast_str(leg):
    if leg["kind"] == 0:
        return f"(0,{leg['path']},({ZERO},{ZERO},0,0,{ZERO}),false)"
    k = leg["key"]
    return (f"(1,0x,({k['c0']},{k['c1']},{k['fee']},{k['tick']},{k['hooks']}),"
            f"{'true' if leg['zf'] else 'false'})")


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
        # Merge-safe: another keeper process (sync vs executor) may have
        # onboarded tokens since we loaded — never clobber a richer entry.
        if self.state_file.exists():
            try:
                disk = json.loads(self.state_file.read_text())
                for k, v in disk.get("token_map", {}).items():
                    mine = self.state["token_map"].get(k)
                    if isinstance(v, dict) and "legs_buy" in v and (
                            not isinstance(mine, dict) or "legs_buy" not in mine):
                        self.state["token_map"][k] = v
            except (ValueError, OSError):
                pass
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
        tok = self.state["token_map"].get(real_addr)
        if self.mainnet and tok is not None and "legs_buy" not in tok:
            tok = None  # stale pre-RouteAdapter entry: rediscover and re-set routes
        if tok is None:
            tok = (self._onboard_mainnet_token(real_addr, symbol) if self.mainnet
                   else self._onboard_testnet_token(real_addr, symbol))
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

    def _deepest_fee(self, token, quote, qprice, qdec):
        """Deepest V3 fee tier for token/quote by USD depth -> (depth_usd, fee) or None."""
        best = None
        for fee in FEE_TIERS:
            pool = self.call(V3_FACTORY, "getPool(address,address,uint24)(address)",
                             token, quote, fee).strip()
            if int(pool, 16) == 0:
                continue
            depth = uint(self.call(quote, "balanceOf(address)(uint256)", pool)) / 10**qdec * qprice
            if best is None or depth > best[0]:
                best = (depth, fee)
        return best

    def _dexscreener_pairs(self, token, label):
        """Uniswap pairs of one version for a token, deepest first:
        [(other_address, liq_usd, pair_id)]."""
        try:
            r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}",
                             headers={"User-Agent": "copy-vault"}, timeout=15)
            pairs = [p for p in (r.json().get("pairs") or [])
                     if p.get("chainId") == "robinhood" and p.get("dexId") == "uniswap"
                     and (p.get("labels") or []) == [label]]
        except requests.RequestException:
            return []
        out = []
        for p in sorted(pairs, key=lambda p: -((p.get("liquidity") or {}).get("usd") or 0)):
            base, quote = p["baseToken"]["address"], p["quoteToken"]["address"]
            other = quote if base.lower() == token.lower() else base
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if liq >= 1000 and other.lower() not in {a.lower() for a, _, _ in out}:
                out.append((other, liq, p["pairAddress"]))
        return out

    def _dexscreener_intermediates(self, token):
        skip = {USDG.lower(), WETH.lower(), token.lower(), ZERO}
        return [(a, liq) for a, liq, _ in self._dexscreener_pairs(token, "v3")
                if a.lower() not in skip][:3]

    def _v4_pool_key(self, pair_id):
        """Resolve a dexscreener v4 pool id -> PoolKey dict via PositionManager."""
        out = self.call(POSITION_MANAGER, "poolKeys(bytes25)(address,address,uint24,int24,address)",
                        pair_id[:52]).splitlines()
        c0, c1, fee, tick, hooks = [x.split()[0] for x in out]
        if int(c0, 16) == 0:
            return None  # native-ETH pool, unsupported by the adapter
        return {"c0": c0, "c1": c1, "fee": int(fee), "tick": int(tick), "hooks": hooks}

    def _v3_prefix_to(self, target, eth_price):
        """Cheapest v3 legs from USDG to `target` (and reverse) or None."""
        t = target.lower()
        if t == USDG.lower():
            return [], []
        if t == WETH.lower():
            uw = self._usdg_weth_fee()
            return ([{"kind": 0, "path": encode_path(USDG, uw, WETH)}],
                    [{"kind": 0, "path": encode_path(WETH, uw, USDG)}])
        d = self._deepest_fee(target, USDG, 1.0, 6)
        if d and d[0] >= 1000:
            return ([{"kind": 0, "path": encode_path(USDG, d[1], target)}],
                    [{"kind": 0, "path": encode_path(target, d[1], USDG)}])
        w = self._deepest_fee(target, WETH, eth_price, 18)
        if w and w[0] >= 1000:
            uw = self._usdg_weth_fee()
            return ([{"kind": 0, "path": encode_path(USDG, uw, WETH, w[1], target)}],
                    [{"kind": 0, "path": encode_path(target, w[1], WETH, uw, USDG)}])
        return None

    def quote_route(self, legs, amount_in):
        """Chain quotes through mixed v3/v4 legs -> final raw amount out."""
        amt = int(amount_in)
        for leg in legs:
            if leg["kind"] == 0:
                amt = self.quote_out(leg["path"], amt)
            else:
                k, zf = leg["key"], "true" if leg["zf"] else "false"
                out = self.call(
                    V4_QUOTER,
                    "quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))(uint256,uint256)",
                    f"(({k['c0']},{k['c1']},{k['fee']},{k['tick']},{k['hooks']}),{zf},{amt},0x)")
                amt = uint(out.splitlines()[0])
        return amt

    def _discover_route(self, token, symbol):
        """Read-only: best USDG<->token route -> (legs_buy, legs_sell, desc, depth_usd).

        Preference order: V3 USDG-direct / WETH-hop / V3-intermediate, then V4
        pools (single leg, with a V3 prefix when the pool is quoted in WETH or
        another routable token — e.g. SIT's hooked V4 pool quoted in AI).
        Every candidate is probe-quoted end-to-end before being accepted.
        """
        eth_price = lib.price_usd(WETH.lower(), "robinhood") or 0
        candidates = []  # (depth, legs_buy, legs_sell, desc)

        def v3_candidate(pb, ps, depth, desc):
            candidates.append((depth, [{"kind": 0, "path": pb}], [{"kind": 0, "path": ps}], desc))

        d = self._deepest_fee(token, USDG, 1.0, 6)
        if d and d[0] >= 1000:
            v3_candidate(encode_path(USDG, d[1], token), encode_path(token, d[1], USDG),
                         d[0], f"v3 USDG direct fee {d[1]}")
        w = self._deepest_fee(token, WETH, eth_price, 18)
        if w and w[0] >= 1000:
            uw = self._usdg_weth_fee()
            v3_candidate(encode_path(USDG, uw, WETH, w[1], token),
                         encode_path(token, w[1], WETH, uw, USDG),
                         w[0], f"v3 WETH hop fee {w[1]}")

        if not candidates:
            for inter, _liq in self._dexscreener_intermediates(token):
                iprice = lib.price_usd(inter.lower(), "robinhood")
                if not iprice:
                    continue
                idec = uint(self.call(inter, "decimals()(uint8)"))
                ti = self._deepest_fee(token, inter, iprice, idec)
                if not ti or ti[0] < 1000:
                    continue
                prefix = self._v3_prefix_to(inter, eth_price)
                if prefix is None:
                    continue
                pre_buy, pre_sell = prefix  # non-empty: intermediates exclude USDG/WETH
                # extend the USDG->inter path by one hop into the token
                buy_path = pre_buy[0]["path"] + ti[1].to_bytes(3, "big").hex() + token[2:].lower()
                sell_path = "0x" + token[2:].lower() + ti[1].to_bytes(3, "big").hex() \
                    + pre_sell[0]["path"][2:]
                v3_candidate(buy_path, sell_path, ti[0], f"v3 via {inter[:10]}.. fee {ti[1]}")
                break

        # V4 fallback: deepest v4 pairs, single v4 leg + v3 prefix to its quote side
        if not candidates:
            for other, liq, pair_id in self._dexscreener_pairs(token, "v4")[:3]:
                if other.lower() == ZERO:
                    continue  # native-ETH v4 pools unsupported
                key = self._v4_pool_key(pair_id)
                if key is None:
                    continue
                if token.lower() not in (key["c0"].lower(), key["c1"].lower()):
                    continue
                prefix = self._v3_prefix_to(other, eth_price)
                if prefix is None:
                    continue
                pre_buy, pre_sell = prefix
                zf_buy = other.lower() == key["c0"].lower()  # buying: in = other side
                legs_buy = pre_buy + [{"kind": 1, "key": key, "zf": zf_buy}]
                legs_sell = [{"kind": 1, "key": key, "zf": not zf_buy}] + pre_sell
                candidates.append((liq, legs_buy, legs_sell,
                                   f"v4 pool vs {other[:10]}.. (hooked)" if int(key["hooks"], 16)
                                   else f"v4 pool vs {other[:10]}.."))
                break

        for depth, lb, ls, desc in sorted(candidates, key=lambda c: -c[0]):
            try:
                if self.quote_route(lb, 5 * 10**6) > 0:
                    return lb, ls, desc, depth
            except Exception:
                continue
        raise RuntimeError(f"no routable Uniswap liquidity for {symbol}")

    def _onboard_mainnet_token(self, token, symbol):
        """Allowlist the real token and configure its route on the RouteAdapter."""
        decimals = uint(self.call(token, "decimals()(uint8)"))
        legs_buy, legs_sell, desc, depth = self._discover_route(token, symbol)
        adapter = self.dep["route_adapter"]
        self.send(adapter, SET_ROUTE_SIG, USDG, token,
                  "[" + ",".join(leg_cast_str(l) for l in legs_buy) + "]")
        self.send(adapter, SET_ROUTE_SIG, token, USDG,
                  "[" + ",".join(leg_cast_str(l) for l in legs_sell) + "]")
        self.send(self.dep["vault"], "setAllowedToken(address,bool)", token, "true")
        tok = {"addr": token, "decimals": decimals, "legs_buy": legs_buy,
               "legs_sell": legs_sell, "pool_depth_usd": round(depth), "route": desc}
        self.state["token_map"][token.lower()] = tok
        self.save()
        print(f"  [exec] onboarded {symbol}: {desc}, depth ${depth:,.0f}")
        return tok

    def min_out(self, tok, side, amount_in):
        """Quoted minimum output for a mainnet trade, slippage-adjusted."""
        legs = tok["legs_buy"] if side == "buy" else tok["legs_sell"]
        return int(self.quote_route(legs, amount_in) * (1 - SLIPPAGE))

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
        if self.mainnet:
            # value the sleeve wallet directly (cash + positions + SOL);
            # the on-chain sleeveFundedAsset counter is only the cap tracker
            import sleeve as sleeve_mod
            pub = sleeve_mod._env().get("SLEEVE_SOLANA_PUBKEY")
            if pub:
                try:
                    nav += sleeve_mod.sleeve_value_usd(self.cfg, pub)
                except Exception as e:
                    print(f"  [exec warn] sleeve valuation failed, NAV excludes sleeve: {e}")
        else:
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
        # shared with sleeve.py for live sizing of Solana copies
        (lib.data_dir(self.cfg) / "nav.json").write_text(
            json.dumps({"nav_usd": nav, "ts": time.time()}))
        print(f"  [exec] posted NAV ${nav:,.2f}")
        return nav

    # ------------------------------------------------------------ trades

    MIN_SIGNAL_LIQUIDITY = 25_000

    def execute(self, rec):
        price = rec.get("detection_price_usd") or rec.get("trader_implied_price_usd")
        if not price or not rec.get("trader_usd"):
            return
        if self.mainnet and rec["side"] == "buy":
            # Airdropped clone tokens masquerade as one-sided buys with fake
            # pricing; only follow buys into pools deep enough to be real and
            # sized plausibly against them.
            info = lib.token_price_info(rec["asset_address"],
                                        self.cfg["chain"]["dexscreener_chain_id"])
            if (info["liquidity"] < self.MIN_SIGNAL_LIQUIDITY
                    or rec["trader_usd"] > info["liquidity"]):
                print(f"  [exec] untrusted signal skipped: {rec['asset_symbol']} "
                      f"(${rec['trader_usd']:,.0f} vs pool ${info['liquidity']:,.0f})")
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
                min_out = self.min_out(tok, "buy", amount_in)
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
                min_out = self.min_out(tok, "sell", amount_in)
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
                            try:
                                self.execute(rec)
                            except Exception as e:
                                print(f"  [exec warn] {rec['asset_symbol']}: {e}")
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
