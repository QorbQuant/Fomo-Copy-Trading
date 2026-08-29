# fomo-copy-vault

Prototype for a single-trader copytrading vault on Robinhood Chain: watch a
fomo trader's wallet on-chain and paper-trade what an ERC-4626 copy vault
would do. **Phase 1 of the plan — no contracts, no real money.** The output of
this phase is a go/no-go number: does copied PnL survive detection latency?

## Target trader

**@AvgJoesCrypto** on [fomo](https://fomo.family) (49K followers).

| chain | address | verification |
|---|---|---|
| EVM (Robinhood Chain, chain id 4663) | `0x06de9c48b1e639ed5c13ec8fbd4080a38e39f2d1` | EIP-7702 delegated account, actively swapping tokenized stocks + memecoins via Relay router |
| Solana (watch-only, not copied) | `H2QSGECp13sFLJgdTsDtayX3dk18Dm6sQMSQKcew7Xzk` | Solscan shows it funded by "Fomo Co-signer", swaps via DFlow/Jupiter every few minutes |

Addresses resolved via fomowalletfinder.com (unofficial) and corroborated
on-chain (Fomo Co-signer funding, fomo-style EIP-7702 setup, activity profile).
**Before trusting any results, spot-check 2–3 recent trades in the fomo app
feed against these addresses.**

## Key mechanics learned

- fomo EVM accounts are **EIP-7702 delegated**; fomo's relayer submits the txs,
  so `tx.from` is never the trader. Detection filters ERC-20 `Transfer` logs
  where the trader is sender or recipient.
- fomo's unified cross-chain balance means some fills are **one-sided** on
  Robinhood Chain (the other leg settles elsewhere via Relay). These are
  copied too, flagged `one_sided`.
- Prices: dexscreener supports Robinhood Chain (`chainId: "robinhood"`), free.
- RPC: `https://rpc.mainnet.chain.robinhood.com` · explorer/API:
  `https://robinhoodchain.blockscout.com`

## Run

```
pip install requests
python watcher.py        # backfills ~20k blocks, then follows live; Ctrl-C to stop
python pnl_report.py     # coverage, latency distribution, hypothetical PnL
```

Vault parameters (paper AUM, max trade fraction, min copy size) are in
`config.json`. Sizing: `copy_usd = aum * min(trade_usd / trader_ref_capital,
max_trade_pct)`.

Outputs in `data/`: `trades.jsonl` (normalized trader activity),
`copy_trades.jsonl` (what the vault would have done, with detection-time fill
prices and latency-drift bps vs the trader's implied execution price).

## Solana sleeve (cross-chain execution)

The vault's Solana arm follows the fomo pattern: a dedicated Solana address
per vault, pre-funded so copies execute at detection latency (bridge time only
affects rebalancing).

- **On-chain (EVM side):** `CopyVault.fundSleeve()` creates a deBridge DLN
  order itself with the receiver pinned to the owner-set sleeve pubkey — the
  executor picks timing/size but can never redirect funds. Capped at
  `sleeveCapBps` of NAV (30%). Mocked DLN on testnet; wire the real DlnSource
  address on chains where deBridge is live.
- **Keeper (`sleeve.py`):** every Solana copy signal is quoted through
  Jupiter (real route + price impact) and logged to `data/sleeve_fills.jsonl`.
  Paper mode by default; real signing with the sleeve keypair only with
  `SLEEVE_EXECUTE=1` + `SLEEVE_SOLANA_SECRET` in `.env` (gitignored).
- **Redemption:** in-kind for Robinhood Chain holdings; the sleeve enters NAV
  as one line and exits cash-settled, bounded by the cap.
- Endgame (not built): replace the sleeve keypair with an Anchor program
  authorized by cross-chain messages — same EVM vault, trust-minimized Solana
  custody.

## Chain split (measured 2026-08-29, 3-day window)

`solana_watcher.py` watches the trader's ~52 Solana token accounts (fomo's
co-signer is a delegate, so wallet-level signature queries miss most swaps —
signatures must be collected per ATA and deduped). Result: **~$33.5K/day
copyable volume on Robinhood Chain vs ~$1K/day on Solana (~97/3)** for this
trader. A cross-chain Solana sleeve is not worth building at this split;
re-check `pnl_report.py` after a week of live data.

## Known limitations (deliberate, prototype)

- Backfilled trades use the trader's implied price as the fill — no latency
  cost modeled; only live-watched trades measure real drift.
- Fill model is the dexscreener price at detection: no pool-depth slippage
  model yet, no gas.
- Trader `ref_capital_usd` is a config constant (~DeBank total), not live.
- Quote-vs-asset leg classification and funding-vs-trade classification use
  liquidity heuristics; check `trades.jsonl` when something looks off.
- Solana side is watch-only and currently not ingested at all.

## Phase 2: the vault contract (`contracts/`)

`CopyVault.sol` — the pooled vault whose shares are the "vault token":

- **Deposits** in one asset (USDC-style); shares priced off a keeper-posted
  NAV with a freshness TTL (stale NAV blocks deposits, never exits).
- **`mirrorTrade()`** — executor-only, one side must be the asset, buys capped
  at `maxTradeBps` of NAV and restricted to an owner-set token allowlist,
  `minOut` slippage bound, sells only of actually-held positions.
- **In-kind redemption** — burning shares pays a pro-rata slice of the asset
  and every held token, so exits never depend on NAV pricing (the main
  NAV-manipulation surface is gone by construction). Plus a withdraw delay.

No performance fees yet; executor/owner are fully trusted (prototype).

```
forge test --root contracts                    # 12 tests
anvil &                                        # or use the Robinhood testnet
forge script contracts/script/Demo.s.sol --root contracts \
  --rpc-url http://127.0.0.1:8545 --private-key <key> --broadcast
```

Testnet (chain id 46630, RPC `https://rpc.testnet.chain.robinhood.com`,
explorer `explorer.testnet.chain.robinhood.com`): throwaway deployer key in
`contracts/.env` (gitignored); fund it at
`https://faucet.testnet.chain.robinhood.com`, then run
`script/Deploy.s.sol` with `--rpc-url $TESTNET_RPC`.

**The loop is closed by `executor.py`**: it tails `data/copy_trades.jsonl`
and, for each live Robinhood Chain copy signal (≤10 min old), deploys a
testnet mock for any unseen token (allowlisting it and pinning the mock
router's rate to the real token's live price), posts NAV computed from actual
on-chain holdings, sizes against on-chain NAV, and sends `mirrorTrade()` with
the keeper key. Executions land in `data/executions.jsonl`.

```
python executor.py --test   # inject one synthetic buy, then follow live
python executor.py          # follow live signals
```

**Initial sync (Hyperliquid-style):** `sync.py` brings the vault to the
trader's current portfolio weights before flow-mirroring takes over — fetches
the live book from Blockscout, computes weights (stables = cash), and
buys/sells in clips within the vault's 5% per-trade cap. Idempotent; rerun to
correct drift. `--dry-run` prints the plan. Pause `executor.py` while it runs
(same keeper key — nonce collisions).
