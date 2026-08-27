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

## Known limitations (deliberate, prototype)

- Backfilled trades use the trader's implied price as the fill — no latency
  cost modeled; only live-watched trades measure real drift.
- Fill model is the dexscreener price at detection: no pool-depth slippage
  model yet, no gas.
- Trader `ref_capital_usd` is a config constant (~DeBank total), not live.
- Quote-vs-asset leg classification and funding-vs-trade classification use
  liquidity heuristics; check `trades.jsonl` when something looks off.
- Solana side is watch-only and currently not ingested at all.

## Phase 2 (only if paper PnL is positive)

`CopyVault.sol` — ERC-4626, executor-only `mirrorTrade()` through the Uniswap
router on Robinhood Chain testnet (chain id 46630), on-chain guardrails: max %
AUM per trade, max slippage bps, per-token cap, token allowlist, deposit
epochs + withdrawal delay. See the plan file for full scope.
