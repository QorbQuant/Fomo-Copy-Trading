"""Dry-run copier: given a normalized trader swap, decide what the vault would do.

Nothing here touches a chain. It sizes the hypothetical copy trade, records the
price at detection time (what the vault would roughly fill at), and logs it.
"""

import lib


def plan_copy(cfg, trade, out_path):
    """trade: normalized swap dict from watcher. Returns the copy record or None."""
    vault = cfg["vault"]
    ref_capital = cfg["trader"]["ref_capital_usd"]

    trade_usd = trade.get("usd_value")
    if not trade_usd:
        record = {**_base(trade), "action": "skip", "reason": "no_usd_value"}
        lib.append_jsonl(out_path, record)
        return None

    fraction = min(trade_usd / ref_capital, vault["max_trade_pct"])
    copy_usd = vault["aum_usd"] * fraction
    if copy_usd < vault["min_copy_usd"]:
        record = {**_base(trade), "action": "skip", "reason": "below_min",
                  "would_copy_usd": round(copy_usd, 2)}
        lib.append_jsonl(out_path, record)
        return None

    # Fill model: the asset leg's dexscreener price at detection time.
    # Latency cost = detection price vs the trader's implied execution price.
    asset = trade["asset_token"]
    detection_price = None
    if not trade.get("backfill"):
        detection_price = lib.price_usd(asset["address"], cfg["chain"]["dexscreener_chain_id"])
    fill_price = detection_price or asset.get("implied_price_usd")

    slippage_bps = None
    if detection_price and asset.get("implied_price_usd"):
        drift = (detection_price - asset["implied_price_usd"]) / asset["implied_price_usd"]
        # buying after price ran up (or selling after it dropped) is the cost
        slippage_bps = round(drift * 10000 * (1 if trade["side"] == "buy" else -1), 1)

    record = {
        **_base(trade),
        "action": "copy",
        "side": trade["side"],
        "copy_usd": round(copy_usd, 2),
        "fraction_of_vault": round(fraction, 6),
        "asset_symbol": asset["symbol"],
        "asset_address": asset["address"],
        "trader_implied_price_usd": asset.get("implied_price_usd"),
        "detection_price_usd": detection_price,
        "fill_price_usd": fill_price,
        "latency_drift_bps": slippage_bps,
        "copy_amount": round(copy_usd / fill_price, 8) if fill_price else None,
    }
    lib.append_jsonl(out_path, record)
    return record


def _base(trade):
    return {
        "tx_hash": trade["tx_hash"],
        "block": trade["block"],
        "block_time": trade["block_time"],
        "detected_at": trade["detected_at"],
        "latency_s": trade.get("latency_s"),
        "backfill": trade.get("backfill", False),
        "trader_usd": trade.get("usd_value"),
    }
