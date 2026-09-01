"""Treasury management (v3 only): realize the vault's accrued AUM + performance
fee shares into operating value — keeper gas and/or protocol-token buybacks.

The v3 vault streams management (AUM) and performance fees to the treasury
address as SHARES (minted via dilution, so no loose USDG is needed to charge
them). This module, run on a cadence, turns those shares into usable value:

  1. vault.accrue()  — crystallize fees up to now (permissionless).
  2. If the treasury's share value >= min_realize_usd, redeem redeem_pct of the
     shares in-kind: the treasury receives its pro-rata basket (USDG + each held
     token). Fee shares carry no withdraw lock (lastDepositAt == 0).
  3. Sell each received token -> USDG through the vault's RouteAdapter, which is
     public and already carries the V3/V4 routes the keeper set for mirroring.
  4. Split the USDG realized this run per config: gas_pct -> keeper ETH
     (USDG->WETH via the adapter, unwrap, deliver to the keeper); buyback_pct ->
     protocol token (dormant until buyback_token + a USDG->token route exist).

Treasury wallet: config "treasury.wallet"; empty defaults to the keeper
(DEPLOYER), whose PRIVATE_KEY then signs. A separate treasury wallet needs its
own key in contracts/.env as TREASURY_PRIVATE_KEY.

Only meaningful once a v3 vault is deployed and deployments.json sets
vault_version:3 — these fee shares do not exist before that.

    python3 treasury.py --status --mainnet
    python3 treasury.py --run --mainnet
"""

import json
import sys
import time

import lib
from executor import Executor, E, WETH, USDG, ZERO, encode_path, run, uint

BURN = "0x000000000000000000000000000000000000dEaD"


class Treasury:
    def __init__(self):
        self.ex = Executor(mainnet=True)
        self.cfg = self.ex.cfg
        self.tcfg = self.cfg.get("treasury", {})
        self.vault = self.ex.dep["vault"]
        self.adapter = self.ex.dep["route_adapter"]
        self.keeper = E["DEPLOYER"]
        self.wallet = self.tcfg.get("wallet") or self.keeper
        # signing key: keeper reuses PRIVATE_KEY; a distinct treasury needs its own
        if self.wallet.lower() == self.keeper.lower():
            self.key = E["PRIVATE_KEY"]
        else:
            self.key = E.get("TREASURY_PRIVATE_KEY")
            if not self.key:
                raise SystemExit("treasury.wallet != keeper but TREASURY_PRIVATE_KEY missing from contracts/.env")

    # ---- chain helpers (sign as the treasury wallet) ----

    def call(self, to, sig, *args):
        return self.ex.call(to, sig, *args)

    def tsend(self, to, sig, *args, value=None):
        cmd = ["cast", "send", to, sig, *[str(a) for a in args],
               "--rpc-url", self.ex.rpc, "--private-key", self.key, "--json"]
        if value:
            cmd += ["--value", str(value)]
        out = json.loads(run(cmd))
        if out.get("status") not in ("0x1", 1, "1"):
            raise RuntimeError(f"tx reverted: {out.get('transactionHash')}")
        return out["transactionHash"]

    def bal(self, token, who=None):
        return uint(self.call(token, "balanceOf(address)(uint256)", who or self.wallet))

    # ---- valuation ----

    def is_v3(self):
        return self.ex.vault_version >= 3

    def held_tokens(self):
        n = uint(self.call(self.vault, "heldTokensLength()(uint256)"))
        return [self.call(self.vault, "heldTokens(uint256)(address)", i).strip() for i in range(n)]

    def treasury_shares(self):
        return self.bal(self.vault)

    def share_value_usd(self, shares):
        nav = uint(self.call(self.vault, "totalNavAsset()(uint256)"))
        supply = uint(self.call(self.vault, "totalSupply()(uint256)"))
        if supply == 0:
            return 0.0
        return shares * nav / supply / 1e6  # USDG has 6 decimals

    # ---- swaps via the (public) RouteAdapter ----

    def _has_route(self, tin, tout):
        try:
            return uint(self.call(self.adapter, "routeLength(address,address)(uint256)", tin, tout)) > 0
        except RuntimeError:
            return False

    def _price(self, token):
        return lib.price_usd(token.lower(), self.cfg["chain"]["dexscreener_chain_id"]) or 0

    def swap_via_adapter(self, tin, tout, amount, min_out, to=None):
        to = to or self.wallet
        self.tsend(tin, "approve(address,uint256)", self.adapter, amount)
        return self.tsend(self.adapter, "swap(address,address,uint256,uint256,address)",
                          tin, tout, amount, min_out, to)

    def sell_token_to_usdg(self, token, amount, decimals):
        """Route a held token back to USDG. minOut from the dexscreener mark."""
        if not self._has_route(token, USDG):
            print(f"  [treasury] no adapter route {token[:10]}->USDG, skipping")
            return 0
        price = self._price(token)
        if not price:
            print(f"  [treasury] no price for {token[:10]}, skipping")
            return 0
        slip = self.tcfg.get("slippage_bps", 300) / 10_000
        min_out = int(amount / 10 ** decimals * price * 1e6 * (1 - slip))
        before = self.bal(USDG)
        self.swap_via_adapter(token, USDG, amount, min_out)
        return self.bal(USDG) - before

    # ---- the run loop ----

    def status(self):
        v3 = self.is_v3()
        shares = self.treasury_shares() if v3 else 0
        val = self.share_value_usd(shares) if shares else 0.0
        print(f"vault v3:            {v3} (version {self.ex.vault_version})")
        print(f"treasury wallet:     {self.wallet}"
              f"{'  (= keeper)' if self.wallet.lower() == self.keeper.lower() else ''}")
        print(f"treasury shares:     {shares / 1e18:,.4f} avgJOE  (~${val:,.2f})")
        print(f"enabled:             {self.tcfg.get('enabled')}")
        print(f"split:               gas {self.tcfg.get('gas_pct', 100)}% / "
              f"buyback {self.tcfg.get('buyback_pct', 0)}%")
        bt = self.tcfg.get("buyback_token") or "(unset)"
        print(f"buyback token:       {bt}"
              f"{'' if bt == '(unset)' else ('  route ' + ('OK' if self._has_route(USDG, bt) else 'MISSING'))}")
        return val

    def realize(self):
        if not self.tcfg.get("enabled"):
            print("  [treasury] disabled (config treasury.enabled=false)")
            return
        if not self.is_v3():
            print(f"  [treasury] vault is v{self.ex.vault_version}, not v3 — no fee shares to realize")
            return

        # 1. crystallize fees up to now
        try:
            self.tsend(self.vault, "accrue()")
        except RuntimeError as e:
            print(f"  [treasury] accrue() failed (continuing): {str(e)[:120]}")

        shares = self.treasury_shares()
        val = self.share_value_usd(shares)
        floor = self.tcfg.get("min_realize_usd", 25)
        if val < floor:
            print(f"  [treasury] value ${val:,.2f} < ${floor} floor — hold")
            return
        redeem_shares = shares * self.tcfg.get("redeem_pct", 100) // 100
        if redeem_shares == 0:
            return

        # 2. redeem in-kind: snapshot USDG first so we can measure realized USDG
        usdg_start = self.bal(USDG)
        tokens = self.held_tokens()
        held_before = {t: self.bal(t) for t in tokens}
        print(f"  [treasury] redeeming {redeem_shares / 1e18:,.4f} shares (~${val * redeem_shares / shares:,.2f})")
        self.tsend(self.vault, "redeemInKind(uint256,address)", redeem_shares, self.wallet)

        # 3. sell each received token -> USDG
        for t in tokens:
            recv = self.bal(t) - held_before.get(t, 0)
            if recv <= 0:
                continue
            dec = uint(self.call(t, "decimals()(uint8)"))
            if recv / 10 ** dec * self._price(t) < 1:  # ignore dust
                continue
            try:
                got = self.sell_token_to_usdg(t, recv, dec)
                print(f"  [treasury] sold {t[:10]} -> ${got / 1e6:,.2f} USDG")
            except RuntimeError as e:
                print(f"  [treasury] sell {t[:10]} failed: {str(e)[:100]}")

        realized = self.bal(USDG) - usdg_start
        if realized <= 0:
            print("  [treasury] nothing realized")
            return
        print(f"  [treasury] realized ${realized / 1e6:,.2f} USDG this run")

        # 4. split -> gas + buyback
        gas_pct = self.tcfg.get("gas_pct", 100)
        buy_pct = self.tcfg.get("buyback_pct", 0)
        denom = gas_pct + buy_pct or 1
        gas_usdg = realized * gas_pct // denom
        buy_usdg = realized - gas_usdg
        log = {"ts": round(time.time(), 3), "realized_usdg": realized / 1e6}

        if gas_usdg > 1e6 * 0.5:  # >$0.50 worth to bother
            try:
                eth_out = self.fund_gas(gas_usdg)
                log["gas_usdg"] = gas_usdg / 1e6
                log["eth_out"] = eth_out
            except RuntimeError as e:
                print(f"  [treasury] gas funding failed: {str(e)[:120]}")

        if buy_usdg > 1e6 * 0.5:
            try:
                bought = self.buyback(buy_usdg)
                if bought is not None:
                    log["buyback_usdg"] = buy_usdg / 1e6
                    log["bought"] = bought
            except RuntimeError as e:
                print(f"  [treasury] buyback failed: {str(e)[:120]}")

        lib.append_jsonl(lib.data_dir(self.cfg) / "treasury.jsonl", log)

    def fund_gas(self, usdg_amount):
        """USDG -> WETH via adapter -> unwrap -> deliver ETH to the keeper."""
        if not self._has_route(USDG, WETH):
            print("  [treasury] no USDG->WETH route, gas funding skipped")
            return 0.0
        eth_price = self._price(WETH)
        slip = self.tcfg.get("slippage_bps", 300) / 10_000
        min_weth = int((usdg_amount / 1e6) / eth_price * 1e18 * (1 - slip)) if eth_price else 0
        weth_before = self.bal(WETH)
        self.swap_via_adapter(USDG, WETH, usdg_amount, min_weth)
        weth_out = self.bal(WETH) - weth_before
        if weth_out <= 0:
            return 0.0
        self.tsend(WETH, "withdraw(uint256)", weth_out)  # unwrap to native ETH
        if self.wallet.lower() != self.keeper.lower():
            self.tsend(self.keeper, "", value=weth_out)  # deliver to the keeper
        print(f"  [treasury] gas: ${usdg_amount / 1e6:,.2f} USDG -> {weth_out / 1e18:.5f} ETH @ keeper")
        return weth_out / 1e18

    def buyback(self, usdg_amount):
        """USDG -> protocol token via adapter. Dormant until configured + routed."""
        token = self.tcfg.get("buyback_token")
        if not token:
            print("  [treasury] buyback_token unset — buyback share held as USDG in treasury")
            return None
        if not self._has_route(USDG, token):
            print(f"  [treasury] no USDG->{token[:10]} route (owner must setRoute) — skipped")
            return None
        price = self._price(token)
        dec = uint(self.call(token, "decimals()(uint8)"))
        slip = self.tcfg.get("slippage_bps", 300) / 10_000
        min_tok = int((usdg_amount / 1e6) / price * 10 ** dec * (1 - slip)) if price else 0
        dest = BURN if self.tcfg.get("burn_buyback") else self.wallet
        before = self.bal(token, dest)
        self.swap_via_adapter(USDG, token, usdg_amount, min_tok, to=dest)
        bought = (self.bal(token, dest) - before) / 10 ** dec
        print(f"  [treasury] buyback: ${usdg_amount / 1e6:,.2f} USDG -> {bought:,.2f} token"
              f"{' (burned)' if dest == BURN else ''}")
        return bought


def main():
    if "--mainnet" not in sys.argv:
        raise SystemExit("treasury.py operates on the mainnet v3 vault: pass --mainnet")
    t = Treasury()
    if "--run" in sys.argv:
        t.realize()
    t.status()


if __name__ == "__main__":
    main()
