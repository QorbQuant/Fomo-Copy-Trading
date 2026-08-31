"""Health monitor + alerting. Runs on a timer; alerts on anything that would
otherwise fail silently: keeper gas low on any chain, NAV gone stale (which
freezes deposits/redeems), a bridge order stuck unfilled, the executor wedged,
or the vault's value moving in a way that shouldn't happen unattended.

Alerts go to ALERT_WEBHOOK (.env) — Slack/Discord/generic incoming-webhook JSON.
State (last-seen values, last-alert times) lives in data/monitor_state.json so
it only pages on a NEW problem, not every tick. Exit code 0 always (a timer
shouldn't flap); severity is in the alert.

    python3 monitor.py            # one health pass, alert on new problems
    python3 monitor.py --report   # print full health, no alerting
"""

import json
import sys
import time

import requests

import lib
import sleeve
from executor import Executor, USDG, uint

# thresholds
GAS_MIN = {"robinhood": 0.004, "base": 0.001, "ethereum": 0.003, "monad": 0.05, "bnb": 0.002}
SOL_MIN = 0.015
NAV_STALE_S = 1800          # on-chain NAV older than this = deposits at risk
FILE_STALE_S = 600          # nav file older than this = executor likely wedged
BRIDGE_STUCK_S = 1800       # a pending bridge older than this = investigate
NAV_DROP_PCT = 0.25         # combined NAV dropping >25% between ticks (unattended) = alarm
REALERT_S = 3600            # don't repeat the same alert more than hourly


def _env():
    return lib._read_env()


def alert(cfg, key, severity, msg, state):
    now = time.time()
    last = state.get("alerts", {}).get(key, 0)
    if now - last < REALERT_S:
        return
    state.setdefault("alerts", {})[key] = now
    line = f"[{severity}] avgJOE vault: {msg}"
    print("ALERT " + line)
    url = _env().get("ALERT_WEBHOOK")
    if url:
        try:
            requests.post(url, json={"text": line}, timeout=10)
        except requests.RequestException:
            pass


def clear(state, key):
    state.get("alerts", {}).pop(key, None)


def check(cfg, report=False):
    ex = Executor(mainnet=True)
    d = lib.data_dir(cfg)
    sf = d / "monitor_state.json"
    state = json.loads(sf.read_text()) if sf.exists() else {}
    lines = []

    # --- keeper gas on every active EVM chain ---
    keeper = lib.rpc(ex.rpc, "eth_getBalance", [ex.dep["deployer_executor"], "latest"])
    rh_eth = int(keeper, 16) / 1e18
    lines.append(f"keeper ETH (robinhood): {rh_eth:.5f}")
    if rh_eth < GAS_MIN["robinhood"]:
        alert(cfg, "gas_robinhood", "warn", f"keeper ETH low on robinhood: {rh_eth:.5f}", state)
    else:
        clear(state, "gas_robinhood")
    for name, sat in cfg.get("satellites", {}).items():
        if not sat.get("live"):
            continue
        bal = int(lib.rpc(sat["rpc"], "eth_getBalance", [ex.dep["deployer_executor"], "latest"]), 16) / 1e18
        lines.append(f"keeper gas ({name}): {bal:.5f}")
        if bal < GAS_MIN.get(name, 0.002):
            alert(cfg, f"gas_{name}", "warn", f"keeper gas low on {name}: {bal:.5f}", state)
        else:
            clear(state, f"gas_{name}")

    # --- sleeve SOL (Solana execution gas) ---
    pub = sleeve._env().get("SLEEVE_SOLANA_PUBKEY")
    if pub:
        sol = sleeve.sol_balance(cfg, pub)
        lines.append(f"sleeve SOL: {sol:.5f}")
        if sol < SOL_MIN:
            alert(cfg, "gas_sol", "warn", f"sleeve SOL low: {sol:.5f}", state)
        else:
            clear(state, "gas_sol")

    # --- on-chain NAV staleness (freezes deposits/redeems) ---
    nav_updated = uint(ex.call(ex.dep["vault"], "navUpdatedAt()(uint256)"))
    nav_age = time.time() - nav_updated
    lines.append(f"on-chain NAV age: {nav_age/60:.0f} min")
    if nav_age > NAV_STALE_S:
        alert(cfg, "nav_stale", "crit", f"on-chain NAV stale {nav_age/60:.0f} min — deposits/redeems "
              f"may revert; executor may be wedged", state)
    else:
        clear(state, "nav_stale")

    # --- executor liveness via the nav file it writes every ~120s ---
    navf = d / "nav_mainnet.json"
    if navf.exists():
        file_age = time.time() - json.loads(navf.read_text())["ts"]
        lines.append(f"nav file age: {file_age/60:.1f} min")
        if file_age > FILE_STALE_S:
            alert(cfg, "exec_wedged", "crit", f"executor not updating NAV file for {file_age/60:.0f} "
                  f"min — process wedged or down", state)
        else:
            clear(state, "exec_wedged")

    # --- stuck bridge order ---
    bp = d / "bridge_pending.json"
    if bp.exists():
        try:
            b = json.loads(bp.read_text())
            age = time.time() - b.get("ts", 0)
            if age > BRIDGE_STUCK_S:
                lines.append(f"bridge pending {age/60:.0f} min: ${b.get('usd')}")
                alert(cfg, "bridge_stuck", "warn", f"bridge order unfilled {age/60:.0f} min "
                      f"(${b.get('usd')}) — check DLN/escrow", state)
            else:
                clear(state, "bridge_stuck")
        except ValueError:
            pass

    # --- NAV sanity: big unexplained drop between ticks ---
    try:
        nav = ex.compute_nav_usd()
        lines.append(f"combined NAV: ${nav:,.2f}")
        prev = state.get("last_nav")
        if prev and nav < prev * (1 - NAV_DROP_PCT):
            alert(cfg, "nav_drop", "crit", f"combined NAV dropped {(1-nav/prev)*100:.0f}% "
                  f"(${prev:,.0f} -> ${nav:,.0f}) since last check", state)
        else:
            clear(state, "nav_drop")
        state["last_nav"] = nav
    except Exception as e:
        lines.append(f"NAV compute failed: {str(e)[:80]}")
        alert(cfg, "nav_compute", "warn", f"NAV computation failing: {str(e)[:80]}", state)

    state["last_check"] = time.time()
    sf.write_text(json.dumps(state, indent=1))
    if report:
        print("=== health ===")
        for l in lines:
            print("  " + l)
    return lines


def main():
    cfg = lib.load_config()
    check(cfg, report="--report" in sys.argv)


if __name__ == "__main__":
    main()
