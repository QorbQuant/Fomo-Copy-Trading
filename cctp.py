"""Circle CCTP V2 bridge rail — native USDC burn-and-mint.

The second bridge rail alongside deBridge. deBridge covers Robinhood + the EVM
majors + Solana but NOT Arc; CCTP covers Arc + the EVM majors + Solana but NOT
Robinhood Chain. So Arc connectivity runs on CCTP, and a Robinhood<->Arc move
is a 2-hop (deBridge Robinhood<->Base, CCTP Base<->Arc).

Flow (EVM source -> EVM dest):
  1. approve USDC to TokenMessengerV2
  2. depositForBurn(...) burns USDC, MessageTransmitter emits the message
  3. poll Circle's attestation API for the signed attestation
  4. receiveMessage(message, attestation) on the destination mints USDC

All EVM chains share the same TokenMessengerV2 / MessageTransmitterV2
addresses (verified on-chain). The keeper key is the same address on every EVM
chain, so it is both burner and mint recipient by default.

Contract/domain/USDC values live in config.json "cctp". Mainnet addresses for
Arc itself are published at the Sept 16 2026 launch — fill the "arc" entry then;
everything else is ready now.

    python3 cctp.py --status
    python3 cctp.py --transfer <src> <dst> <usd>        # live; needs funded keeper
"""

import json
import subprocess
import sys
import time

import requests

import lib
from executor import E, run, uint

# Shared across all EVM chains (verified: TokenMessengerV2.localMessageTransmitter
# == MessageTransmitterV2 on Base and Ethereum).
TOKEN_MESSENGER = "0x28b5a0e9c621a5badaa536219b3a228c8168cf5d"
MESSAGE_TRANSMITTER = "0x81D40F21F12A8F0E3252Bccb954D722d4c464B64"
IRIS_MAINNET = "https://iris-api.circle.com"
IRIS_SANDBOX = "https://iris-api-sandbox.circle.com"

DEPOSIT_FOR_BURN = ("depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)")
RECEIVE_MESSAGE = "receiveMessage(bytes,bytes)"
# Fast Transfer where offered, else the chain settles at standard finality.
FINALITY_FAST = 1000
FINALITY_STANDARD = 2000


def _cctp_cfg(cfg):
    return cfg.get("cctp", {})


def chain(cfg, name):
    c = _cctp_cfg(cfg)["chains"].get(name)
    if not c:
        raise SystemExit(f"no cctp config for chain '{name}' "
                         f"(known: {list(_cctp_cfg(cfg)['chains'])})")
    return c


def pad32(addr):
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def cast_send(rpc, to, sig, *args, value=None):
    cmd = ["cast", "send", to, sig, *[str(a) for a in args],
           "--rpc-url", rpc, "--private-key", E["PRIVATE_KEY"], "--json"]
    if value:
        cmd += ["--value", value]
    out = json.loads(run(cmd))
    if out.get("status") not in ("0x1", 1, "1"):
        raise RuntimeError(f"tx reverted: {out.get('transactionHash')}")
    return out["transactionHash"]


def cast_call(rpc, to, sig, *args):
    return run(["cast", "call", to, sig, *[str(a) for a in args], "--rpc-url", rpc])


def iris_base(cfg):
    return IRIS_SANDBOX if _cctp_cfg(cfg).get("testnet") else IRIS_MAINNET


def poll_attestation(cfg, src_domain, burn_tx, timeout=300):
    """Poll Circle for the attestation of a burn. Returns (message, attestation)
    or (None, None) on timeout. status must be 'complete'."""
    url = f"{iris_base(cfg)}/v2/messages/{src_domain}"
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(url, params={"transactionHash": burn_tx}, timeout=20,
                             headers={"User-Agent": "copy-vault"})
            if r.status_code == 200:
                for m in r.json().get("messages", []):
                    if m.get("status") == "complete" and m.get("attestation") not in (None, "PENDING"):
                        return m["message"], m["attestation"]
        except requests.RequestException:
            pass
        time.sleep(6)
    return None, None


def transfer(cfg, src, dst, usd, recipient=None, fast=True):
    """Burn `usd` USDC on `src`, mint it to `recipient` (default keeper) on `dst`."""
    s, d = chain(cfg, src), chain(cfg, dst)
    recipient = recipient or E["DEPLOYER"]
    amount = int(usd * 1e6)
    max_fee = int(amount * 0.001) if fast else 0  # ≤10 bps fast-transfer allowance
    finality = FINALITY_FAST if fast and s.get("fast") else FINALITY_STANDARD

    # 1. approve + 2. burn
    cast_send(s["rpc"], s["usdc"], "approve(address,uint256)", TOKEN_MESSENGER, amount)
    burn_tx = cast_send(s["rpc"], TOKEN_MESSENGER, DEPOSIT_FOR_BURN,
                        amount, d["domain"], pad32(recipient), s["usdc"],
                        pad32("0x0000000000000000000000000000000000000000"),  # any caller
                        max_fee, finality)
    print(f"  [cctp] burned ${usd:,.2f} USDC on {src} ({burn_tx[:14]}...) — attesting...")

    # 3. attestation
    message, attestation = poll_attestation(cfg, s["domain"], burn_tx)
    if not message:
        print(f"  [cctp] no attestation in time — burn stands, mint later with {burn_tx}")
        return {"burn_tx": burn_tx, "minted": False}

    # 4. mint on destination
    mint_tx = cast_send(d["rpc"], MESSAGE_TRANSMITTER, RECEIVE_MESSAGE, message, attestation)
    print(f"  [cctp] minted ${usd:,.2f} USDC to {recipient} on {dst} ({mint_tx[:14]}...)")
    lib.append_jsonl(lib.data_dir(cfg) / "cctp_transfers.jsonl",
                     {"ts": round(time.time(), 3), "src": src, "dst": dst, "usd": round(usd, 2),
                      "burn_tx": burn_tx, "mint_tx": mint_tx})
    return {"burn_tx": burn_tx, "mint_tx": mint_tx, "minted": True}


def verify(cfg):
    """Read-only preflight: contracts present, USDC addrs valid, API reachable."""
    ok = True
    for name, c in _cctp_cfg(cfg)["chains"].items():
        if not c.get("rpc") or not c.get("usdc"):
            print(f"  {name:10} config incomplete (rpc/usdc) — fill at launch" if name == "arc"
                  else f"  {name:10} INCOMPLETE")
            continue
        try:
            code = cast_call(c["rpc"], TOKEN_MESSENGER, "localMessageTransmitter()(address)")
            match = code.strip().lower() == MESSAGE_TRANSMITTER.lower()
            dec = uint(cast_call(c["rpc"], c["usdc"], "decimals()(uint8)"))
            print(f"  {name:10} domain {c['domain']:>2}  TokenMessenger✓ "
                  f"transmitter={'✓' if match else '✗'}  USDC({dec}d)✓  fast={c.get('fast', False)}")
            ok = ok and match
        except Exception as e:
            print(f"  {name:10} CHECK FAILED: {str(e)[:80]}")
            ok = False
    api = iris_base(cfg)
    r = requests.get(f"{api}/v2/messages/0", params={"transactionHash": "0x" + "00" * 32},
                     timeout=15, headers={"User-Agent": "cv"})
    print(f"  attestation API {api}: {'reachable' if r.status_code in (200, 404) else r.status_code}")
    return ok


def main():
    cfg = lib.load_config()
    if "--transfer" in sys.argv:
        i = sys.argv.index("--transfer")
        src, dst, usd = sys.argv[i + 1], sys.argv[i + 2], float(sys.argv[i + 3])
        print(transfer(cfg, src, dst, usd))
    else:
        print("=== CCTP preflight ===")
        verify(cfg)


if __name__ == "__main__":
    main()
