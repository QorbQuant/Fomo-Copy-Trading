"""Live executor: turns copy signals into real CopyVault transactions.

Tails data/copy_trades.jsonl for live (non-backfill) Robinhood Chain copy
records and, for each one:

  1. ensures a testnet mock exists for the real token (deploys MockERC20,
     seeds the mock router, allowlists it in the vault — recorded in state),
  2. pins the mock router's rate to the real token's detection-time price,
     so testnet fills mirror mainnet reality,
  3. posts NAV computed from actual on-chain vault holdings,
  4. sizes the trade against on-chain NAV and sends mirrorTrade().

Signing is delegated to `cast`/`forge` with the keeper key from
contracts/.env. Executions are logged to data/executions.jsonl.

Usage:
    python executor.py           # follow live signals
    python executor.py --test    # inject one synthetic buy first (E2E check)
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import lib

ROOT = Path(__file__).parent
DEPLOY = json.loads((ROOT / "contracts" / "deployments.json").read_text())["robinhood-testnet"]
MAX_TRADE_FRACTION = 0.049  # stay under the vault's 5% on-chain cap
SLIPPAGE = 0.03
MAX_SIGNAL_AGE_S = 600  # never execute a signal detected more than 10 min ago


def env():
    out = {}
    for line in (ROOT / "contracts" / ".env").read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


E = env()
RPC = DEPLOY["rpc"]


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}...: {p.stderr.strip()[:300]}")
    return p.stdout.strip()


def cast_call(to, sig, *args):
    return run(["cast", "call", to, sig, *[str(a) for a in args], "--rpc-url", RPC])


def cast_send(to, sig, *args, value=None):
    cmd = ["cast", "send", to, sig, *[str(a) for a in args],
           "--rpc-url", RPC, "--private-key", E["PRIVATE_KEY"], "--json"]
    if value:
        cmd += ["--value", value]
    out = json.loads(run(cmd))
    if out.get("status") not in ("0x1", 1, "1"):
        raise RuntimeError(f"tx reverted: {out.get('transactionHash')}")
    return out["transactionHash"]


def uint(s):
    return int(s.split()[0])


class Executor:
    def __init__(self):
        self.cfg = lib.load_config()
        self.state_file = lib.data_dir(self.cfg) / "executor_state.json"
        self.state = {"offset": 0, "token_map": {}, "last_nav_post": 0}
        if self.state_file.exists():
            self.state.update(json.loads(self.state_file.read_text()))

    def save(self):
        self.state_file.write_text(json.dumps(self.state, indent=1))

    # ------------------------------------------------------------ tokens

    def ensure_token(self, real_addr, symbol, price):
        """Deploy/allowlist a testnet mock for a real token; pin router rate."""
        if real_addr in self.state["token_map"]:
            mock = self.state["token_map"][real_addr]
        else:
            name = "m" + symbol[:14]
            out = run(["forge", "create", "--root", str(ROOT / "contracts"),
                       "src/mocks/Mocks.sol:MockERC20",
                       "--rpc-url", RPC, "--private-key", E["PRIVATE_KEY"], "--broadcast", "--json",
                       "--constructor-args", name, name, "18"])
            mock = json.loads(out)["deployedTo"]
            cast_send(mock, "mint(address,uint256)", DEPLOY["router"], 10**30)
            cast_send(DEPLOY["mUSDC"], "mint(address,uint256)", DEPLOY["router"], 10**15)
            cast_send(DEPLOY["vault"], "setAllowedToken(address,bool)", mock, "true")
            self.state["token_map"][real_addr] = mock
            self.save()
            print(f"  [exec] deployed {name} -> {mock}")
        # rates at the real token's current price: USDC(6d) <-> mock(18d)
        cast_send(DEPLOY["router"], "setRate(address,address,uint256)",
                  DEPLOY["mUSDC"], mock, int(1e30 / price))
        cast_send(DEPLOY["router"], "setRate(address,address,uint256)",
                  mock, DEPLOY["mUSDC"], int(price * 1e6))
        return mock

    # ------------------------------------------------------------ NAV

    def compute_nav_usd(self):
        nav = uint(cast_call(DEPLOY["mUSDC"], "balanceOf(address)(uint256)", DEPLOY["vault"])) / 1e6
        nav += uint(cast_call(DEPLOY["vault"], "sleeveFundedAsset()(uint256)")) / 1e6
        for real_addr, mock in self.state["token_map"].items():
            bal = uint(cast_call(mock, "balanceOf(address)(uint256)", DEPLOY["vault"])) / 1e18
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
        cast_send(DEPLOY["vault"], "postNav(uint256)", int(nav * 1e6))
        self.state["last_nav_post"] = time.time()
        self.save()
        print(f"  [exec] posted NAV ${nav:,.2f}")
        return nav

    # ------------------------------------------------------------ trades

    def execute(self, rec):
        price = rec.get("detection_price_usd") or rec.get("trader_implied_price_usd")
        if not price or not rec.get("trader_usd"):
            return
        mock = self.ensure_token(rec["asset_address"], rec["asset_symbol"], price)
        self.post_nav(force=True)
        nav = self.compute_nav_usd()

        fraction = min(rec["trader_usd"] / self.cfg["trader"]["ref_capital_usd"], MAX_TRADE_FRACTION)
        usd = nav * fraction
        if usd < 1:
            return

        if rec["side"] == "buy":
            amount_in = int(usd * 1e6)
            min_out = int(usd / price * 1e18 * (1 - SLIPPAGE))
            tx = cast_send(DEPLOY["vault"], "mirrorTrade(address,address,uint256,uint256)",
                           DEPLOY["mUSDC"], mock, amount_in, min_out)
        else:
            bal = uint(cast_call(mock, "balanceOf(address)(uint256)", DEPLOY["vault"]))
            amount_in = min(bal, int(usd / price * 1e18))
            if amount_in == 0:
                return
            min_out = int(amount_in / 1e18 * price * 1e6 * (1 - SLIPPAGE))
            tx = cast_send(DEPLOY["vault"], "mirrorTrade(address,address,uint256,uint256)",
                           mock, DEPLOY["mUSDC"], amount_in, min_out)

        record = {"ts": round(time.time(), 3), "signal_tx": rec["tx_hash"], "side": rec["side"],
                  "symbol": rec["asset_symbol"], "usd": round(usd, 2), "price": price,
                  "vault_tx": tx, "mock": mock}
        lib.append_jsonl(lib.data_dir(self.cfg) / "executions.jsonl", record)
        print(f"  [exec] {rec['side']} ${usd:,.2f} {rec['asset_symbol']} on-chain: {tx}")

    # ------------------------------------------------------------ loop

    def follow(self):
        src = lib.data_dir(self.cfg) / "copy_trades.jsonl"
        print(f"Executor following {src} against vault {DEPLOY['vault']}")
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


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    ex = Executor()
    if "--test" in sys.argv:
        # synthetic signal: buy $2,000 of real CHILL at its live price
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
