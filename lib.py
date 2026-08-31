"""Shared helpers: JSON-RPC, ERC-20 metadata, dexscreener prices, jsonl logs."""

import json
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_session = requests.Session()
_session.headers["User-Agent"] = "fomo-copy-vault-prototype/0.1"


def load_config():
    return json.loads((ROOT / "config.json").read_text())


def data_dir(cfg):
    d = ROOT / cfg["watcher"]["data_dir"]
    d.mkdir(exist_ok=True)
    return d


def append_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")


def read_jsonl(path):
    if not Path(path).exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------- JSON-RPC

def rpc(url, method, params, retries=3):
    for attempt in range(retries):
        try:
            r = _session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                raise RuntimeError(f"RPC error for {method}: {body['error']}")
            return body["result"]
        except (requests.RequestException, RuntimeError):
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def pad_address(addr):
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


_LOG_LIMIT_HINTS = ("limit exceeded", "range", "too large", "too many",
                    "response size", "query returned more", "-32005", "block range")


def _get_logs_adaptive(rpc_url, topics, from_block, to_block):
    """eth_getLogs with automatic range-splitting: public RPCs cap the block
    range / result count differently per chain, so on a limit error we halve
    the range and recurse instead of hard-coding a per-chain chunk size."""
    params = {"fromBlock": hex(from_block), "toBlock": hex(to_block), "topics": topics}
    try:
        return rpc(rpc_url, "eth_getLogs", [params], retries=2)
    except (RuntimeError, requests.HTTPError) as e:
        is_size = any(h in str(e).lower() for h in _LOG_LIMIT_HINTS) or (
            isinstance(e, requests.HTTPError) and getattr(e.response, "status_code", 0)
            in (413, 400))
        if not is_size:
            raise
        if to_block - from_block <= 25:
            # RPC caps below a usable range — skip this slice rather than hang
            print(f"  [warn] getLogs cap too low, skipping blocks {from_block}-{to_block}")
            return []
        mid = (from_block + to_block) // 2
        return (_get_logs_adaptive(rpc_url, topics, from_block, mid)
                + _get_logs_adaptive(rpc_url, topics, mid + 1, to_block))


def get_transfer_logs(rpc_url, address, from_block, to_block):
    """All ERC-20 Transfer logs where `address` is sender or recipient.

    The trader account is EIP-7702 delegated (fomo's relayer submits the txs),
    so we must filter on Transfer topics, not on tx.from.
    """
    logs = []
    for topics in ([TRANSFER_TOPIC, pad_address(address)],
                   [TRANSFER_TOPIC, None, pad_address(address)]):
        logs.extend(_get_logs_adaptive(rpc_url, topics, from_block, to_block))
    seen = set()
    unique = []
    for lg in logs:
        key = (lg["transactionHash"], lg["logIndex"])
        if key not in seen:
            seen.add(key)
            unique.append(lg)
    return unique


_block_ts_cache = {}


def block_timestamp(rpc_url, block_hex):
    if block_hex not in _block_ts_cache:
        blk = rpc(rpc_url, "eth_getBlockByNumber", [block_hex, False])
        _block_ts_cache[block_hex] = int(blk["timestamp"], 16)
    return _block_ts_cache[block_hex]


# ---------------------------------------------------------------- ERC-20 metadata

_TOKENS_FILE = ROOT / "data" / "tokens.json"
_tokens_cache = None


def _eth_call(rpc_url, to, selector):
    return rpc(rpc_url, "eth_call", [{"to": to, "data": selector}, "latest"])


def _decode_string(hexdata):
    raw = bytes.fromhex(hexdata.replace("0x", ""))
    if len(raw) == 32:  # bytes32-style symbol
        return raw.rstrip(b"\x00").decode("utf-8", "replace")
    length = int.from_bytes(raw[32:64], "big")
    return raw[64:64 + length].decode("utf-8", "replace")


def token_meta(rpc_url, token):
    global _tokens_cache
    if _tokens_cache is None:
        _tokens_cache = json.loads(_TOKENS_FILE.read_text()) if _TOKENS_FILE.exists() else {}
    token = token.lower()
    if token not in _tokens_cache:
        try:
            symbol = _decode_string(_eth_call(rpc_url, token, "0x95d89b41"))  # symbol()
            decimals = int(_eth_call(rpc_url, token, "0x313ce567"), 16)       # decimals()
        except Exception:
            symbol, decimals = token[:10], 18
        _tokens_cache[token] = {"symbol": symbol, "decimals": decimals}
        _TOKENS_FILE.parent.mkdir(exist_ok=True)
        _TOKENS_FILE.write_text(json.dumps(_tokens_cache, indent=1))
    return _tokens_cache[token]


# ---------------------------------------------------------------- dexscreener prices

_price_cache = {}  # token -> (ts, {"price":..., "liquidity":...})
PRICE_TTL = 20


def token_price_info(token, chain_slug):
    """{"price": usd or None, "liquidity": usd, "symbol": str or None} from the
    deepest dexscreener pair on this chain. Solana mints are case-sensitive, so
    only EVM addresses are lowercased."""
    key = token.lower() if token.startswith("0x") else token
    now = time.time()
    if key in _price_cache and now - _price_cache[key][0] < PRICE_TTL:
        return _price_cache[key][1]
    info = {"price": None, "liquidity": 0.0, "symbol": None, "volume24h": 0.0,
            "buys24h": 0, "sells24h": 0}
    try:
        r = _session.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}", timeout=15)
        r.raise_for_status()
        pairs = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == chain_slug]
        # aggregate sells across ALL pairs — a honeypot check must see the whole
        # token, not just its deepest pool
        buys24 = sum((p.get("txns", {}).get("h24", {}) or {}).get("buys") or 0 for p in pairs)
        sells24 = sum((p.get("txns", {}).get("h24", {}) or {}).get("sells") or 0 for p in pairs)
        best = None
        for p in pairs:
            liq = (p.get("liquidity") or {}).get("usd") or 0
            vol = (p.get("volume") or {}).get("h24") or 0
            base = p.get("baseToken", {})
            quote = p.get("quoteToken", {})
            px, sym = None, None
            if _addr_eq(base.get("address", ""), token):
                px, sym = p.get("priceUsd"), base.get("symbol")
            elif _addr_eq(quote.get("address", ""), token) and p.get("priceUsd") and p.get("priceNative"):
                sym = quote.get("symbol")
                try:  # price of quote token = priceUsd / priceNative
                    px = float(p["priceUsd"]) / float(p["priceNative"])
                except (ValueError, ZeroDivisionError):
                    px = None
            if px is not None and (best is None or liq > best[0]):
                best = (liq, float(px), sym, vol)
        if best:
            info = {"price": best[1], "liquidity": best[0], "symbol": best[2],
                    "volume24h": best[3], "buys24h": buys24, "sells24h": sells24}
    except requests.RequestException:
        pass
    _price_cache[key] = (now, info)
    return info


def _addr_eq(a, b):
    return a.lower() == b.lower() if b.startswith("0x") else a == b


def price_usd(token, chain_slug):
    return token_price_info(token, chain_slug)["price"]


def honeypot_reason(info):
    """Return a reason string if a token looks unsellable / spoof-baited, else
    None. Guards against honeypots (buyable, not sellable) and tokens seeded
    into the trader's wallet to fake a buy — both show as near-zero sells
    despite many buys. Outcome-based, so it catches the trap regardless of
    whether the trade was real or spoofed."""
    b, s = info.get("buys24h", 0), info.get("sells24h", 0)
    if b >= 10 and s == 0:
        return f"0 sells against {b} buys (honeypot signature)"
    if b >= 30 and s > 0 and b / s > 25:
        return f"buys/sells {b}/{s} = {b/s:.0f}x (near-unsellable)"
    return None
