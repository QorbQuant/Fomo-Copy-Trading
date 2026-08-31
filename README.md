# fomo-copy-vault

A live, single-trader copytrading vault on **Robinhood Chain mainnet** that
autonomously mirrors a [fomo](https://fomo.family) trader's full portfolio
across Robinhood Chain **and** Solana. Deposit USDG, receive `avgJOE` vault
shares, and the vault tracks the trader's positions — same-chain trades in
seconds, cross-chain rotations through a bridge, gas and capital maintained
from its own treasury.

> **Status:** running on mainnet with real funds as a personal vault. It is
> unaudited prototype software. See [Trust & risk](#trust--risk) before
> anyone else's money touches it.

## How it works

```
@AvgJoesCrypto trades (Robinhood Chain or Solana)
   │
   ├─ watcher.py / solana_watcher.py   detect the swap on-chain (~5s)
   │
   ├─ copier.py                        size the copy vs the vault's NAV
   │
   └─ executor.py                      execute on-chain, within guardrails:
        ├─ Robinhood Chain  → CopyVault.mirrorTrade() via Uniswap V3/V4
        ├─ Solana           → sleeve wallet swaps via Jupiter
        ├─ rotations        → deBridge order bridges + swaps in one fill
        └─ treasury         → auto-refills keeper ETH + sleeve SOL from USDC
```

Two loops run continuously: a **reflex** loop (the watchers → executor) mirrors
each trade fast but capped at 5% of NAV per trade, and a **convergence** loop
(`sync.py`) periodically pulls the vault's weights all the way to the trader's,
including cross-chain rebalancing. Big moves complete through convergence; the
cap is the anti-manipulation rail.

## The vault token (`contracts/CopyVault.sol`)

`avgJOE` is an **ERC-20** minted on deposit — your share of the pool. The
contract is **ERC-4626-*style*** (deposit returns shares, keeper-posted NAV
prices them) but deliberately **not** 4626-compliant: it holds many tokens at
once, so instead of single-asset `redeem()` it uses **`redeemInKind()`** —
burning shares pays a pro-rata slice of *every* token the vault holds. Exits
therefore never depend on NAV pricing, which removes the main manipulation
surface by construction.

On-chain guardrails, all enforced in Solidity:

- **`mirrorTrade()`** — executor-only; one side must be the asset (USDG); buys
  capped at `maxTradeBps` (5%) of NAV and restricted to an owner-set token
  allowlist; `minOut` slippage bound; sells only of held positions.
- **`fundSleeve()`** — creates a deBridge DLN order the vault signs itself,
  receiver **pinned** to the Solana sleeve pubkey (the keeper picks timing/size
  but can never redirect funds), capped at `sleeveCapBps` (30%) of NAV.
- **NAV freshness** — stale NAV blocks *deposits*, never redemptions.
- **Withdraw delay** after a deposit; reentrancy guards throughout.

Trust posture today: the **executor/owner is a single trusted keeper key**. No
performance fees. See [Trust & risk](#trust--risk).

## Execution routing

Robinhood Chain liquidity spans Uniswap V3 and V4, and many memecoins hold
liquidity *only* in hooked V4 pools quoted in another memecoin (e.g. SIT/AI).
`contracts/RouteAdapter.sol` executes each trade through a sequence of mixed
legs — V3 multihop (`SwapRouter02`) and single V4 pools (`PoolManager`,
hooked/dynamic-fee included). Routes are discovered by depth, quoted through
`QuoterV2`/`V4Quoter` for `minOut`, and validated leg-by-leg on-chain so a bad
route can't misroute a trade. Reverting routes get a 6-hour cooldown instead of
crashing the run.

## Cross-chain (the Solana sleeve)

The sleeve is a dedicated Solana wallet — the vault's execution arm *and* its
ops treasury — pre-funded so Solana copies execute at detection latency
(bridging only affects rebalancing, which isn't latency-sensitive).

- **Execution:** `sleeve.py` quotes and signs Jupiter swaps for Solana copy
  signals.
- **Rotations:** a big cross-chain move (sell PONS on Robinhood → buy BONK on
  Solana) is one vault-created deBridge order that **gives USDG and receives
  the target token** at the sleeve — the solver does the swap. One fee, one
  wait, delivered as the position.
- **Two-way capital:** `sync.py` bridges idle sleeve USDC back to the vault
  (delivered to the vault contract, cap counter credited via
  `noteSleeveReturn`) when Robinhood buys are cash-short, and out to the sleeve
  when Solana needs cash. Capital auto-balances across chains.
- **Redemption:** in-kind for Robinhood Chain holdings; the sleeve enters NAV
  as one line, cash-settled on exit, bounded by the cap.

## Gas & treasury self-maintenance (`gas.py`)

The executor checks both tanks every NAV cycle and refills from sleeve USDC:
keeper ETH via a deBridge order (Solana → Robinhood Chain, native ETH to the
keeper), sleeve SOL via a Jupiter swap. Bounded by a daily USD cap, floored so
the treasury never fully drains, logged to `data/gas_refills.jsonl`. Gas is a
deposit-funded operating expense, borne pro-rata by NAV.

## Anti-poisoning

Anyone can airdrop clone tokens (fake PONS/CASHBIRD with spoofed pricing) into
the trader's public wallet. Both the book computation and live buy signals
require a position's deepest pool to be real-money deep (≥ $25K) and the
holding to be worth no more than that pool — so the vault never sells real
assets to chase a scam. One-sided outbound transfers whose counterparties are
all plain wallets (not router contracts) are classified as transfers, not
sells, so a wallet migration never triggers a position dump.

## Deployed contracts (Robinhood Chain mainnet, chain id 4663)

| Contract | Address |
|---|---|
| `CopyVault` (`avgJOE`) | `0x12b508A1883b910a537c25883AE7DB518c1511D9` |
| `RouteAdapter` | `0x13c2aeD11ec90f6B8b5Ca8D7Ae1050C2e55195fD` |
| Asset (USDG) | `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` |
| Solana sleeve | `3CMFpnWW5eHSS1sr5gUadNQX2CDA5uweGgiH13f823WC` |

Explorer: `https://robinhoodchain.blockscout.com` · full list in
`contracts/deployments.json`.

## Target trader

**@AvgJoesCrypto** on fomo (~49K followers).

| chain | address |
|---|---|
| Robinhood Chain | `0x06de9c48b1e639ed5c13ec8fbd4080a38e39f2d1` (EIP-7702 delegated — fomo's relayer submits txs, so detection filters ERC-20 `Transfer` logs, not `tx.from`) |
| Solana | `H2QSGECp13sFLJgdTsDtayX3dk18Dm6sQMSQKcew7Xzk` (watched per-ATA — the co-signer delegate hides swaps from wallet-level signature queries) |

Addresses resolved via fomowalletfinder.com and corroborated on-chain (Fomo
Co-signer funding, EIP-7702 setup, activity profile).

## Running it

Python 3.9+, `pip install requests solders base58`, and Foundry (`cast`/`forge`
— the executor shells out to `cast` for EVM txs). Secrets live in `.env`
(sleeve Solana key) and `contracts/.env` (keeper EVM key), both gitignored.

```bash
# initial / drift-correcting rebalance to the trader's weights (pause executor first)
python3 sync.py --mainnet            # --dry-run to preview the plan

# the live mirroring loop (leave running)
python3 executor.py --mainnet

# operational tools
python3 report.py --loop             # 10-min "AJC did X, vault did Y" digest
python3 chart.py                     # time-weighted return chart -> vault_pnl.html
python3 gas.py --status              # treasury / gas tanks
```

Config knobs in `config.json`: `vault` (caps, delays), `sleeve` (`auto_bridge`,
buffer %, min bridge size), `gas` (refill thresholds, daily cap).

### Production deployment (`deploy/`)

Run the three processes as supervised systemd services on a small Ubuntu VM
(auto-restart, boot-persistent, journald logs) instead of a laptop:

```bash
bash deploy/migrate.sh root@YOUR.VM.IP    # stops local procs, syncs, starts services
```

See `deploy/README-deploy.md`. Contracts: `forge test --root contracts`
(includes mainnet fork tests against live Uniswap V3/V4 pools).

## Trust & risk

Deliberately honest about what this is:

- **Unaudited.** Two adversarial review passes (see git history) found and
  fixed real bugs; that is not an audit.
- **Single trusted keeper key**, stored as a plaintext file. A compromised key
  can make bad-but-bounded trades (allowlist + 5% cap + pinned bridge receiver)
  but **cannot block your in-kind redemption** — that's the guarantee that
  matters. KMS/HSM custody is the pre-outside-money upgrade.
- **NAV is keeper-posted** from dexscreener prices — the residual manipulation
  surface (deposits only; exits are in-kind). On-chain NAV bounds are a
  known TODO before outside depositors.
- **Concentrated & young.** This trader is heavily single-token (PONS); returns
  reflect a short, concentrated window.
- **Not compliant advice or a solicitation.** A pooled discretionary vehicle
  for other people's money has regulatory implications not addressed here.

## Roadmap

- Supervised VM (kit built in `deploy/`) — the immediate operational step.
- On-chain NAV sanity bounds; performance fee (high-water mark).
- Anchor program to replace sleeve-keypair custody (trust-minimized Solana).
- Vault factory + registry for "any fomo trader"; automated handle→wallet
  resolution; trader opt-in/staking (Hyperliquid-style anti-gaming).
- Dedicated RPC endpoint (the public one lags across replicas).
