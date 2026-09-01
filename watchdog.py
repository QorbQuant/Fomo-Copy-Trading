"""External watchdog: restart the executor if it hangs — alive but not making
progress. systemd's Restart= only catches crashes; a wedged process (stuck on a
blocking RPC, a deadlocked loop) keeps running while the vault silently stops
updating NAV. Runs as ROOT on a short timer so it can restart the service.

Safety: it only restarts when the executor is systemd-'active' AND its NAV file
has gone stale past STALE_S. If the service is inactive (deliberately stopped for
a manual sync, or crashed and already being handled by Restart=), it does
nothing — so it can never race a running sync.py for the keeper nonce, and a
restart is a plain stop-then-start (never two keepers at once).

STALE_S is set comfortably above the longest legitimate blocking op (the ~300s
deBridge gas-refill wait) so a busy executor is never killed mid-work.
"""

import json
import subprocess
import time
from pathlib import Path

ROOT = Path("/home/fomo/fomo-copy-vault")
NAVF = ROOT / "data" / "nav_mainnet.json"
STALE_S = 720  # 12 min: > the 300s deBridge refill wait + margin


def _webhook():
    try:
        for line in (ROOT / "contracts" / ".env").read_text().splitlines():
            if line.startswith("ALERT_WEBHOOK="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _notify(msg):
    url = _webhook()
    if not url:
        return
    try:
        import requests
        key = "content" if "discord" in url else "text"
        requests.post(url, json={key: msg}, timeout=10)
    except Exception:
        pass


def _active():
    r = subprocess.run(["systemctl", "is-active", "fomo-executor"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def main():
    if _active() != "active":
        return  # stopped for a sync, or crashed (Restart= handles a crash)
    if not NAVF.exists():
        return
    try:
        age = time.time() - json.loads(NAVF.read_text())["ts"]
    except (ValueError, KeyError, OSError):
        return
    if age > STALE_S:
        msg = (f"⚠️ watchdog: executor active but NAV file stale "
               f"{age / 60:.0f} min — restarting fomo-executor")
        print(f"[watchdog] {msg}")
        subprocess.run(["systemctl", "restart", "fomo-executor"], check=False)
        _notify(msg)


if __name__ == "__main__":
    main()
