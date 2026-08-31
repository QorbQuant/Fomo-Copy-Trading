"""Activity report: what AJC did, and what the vault did about it.

Correlates detected trader activity (trades.jsonl) with the vault's responses
(executions_mainnet.jsonl, sleeve_fills.jsonl) over a time window.

    python3 report.py             # last 10 minutes, once
    python3 report.py --loop      # print a fresh report every 10 minutes
    python3 report.py --window 3600
"""

import json
import sys
import time

import lib

WINDOW = 600


def one_report(cfg, window):
    d = lib.data_dir(cfg)
    now = time.time()
    cutoff = now - window
    t = lambda ts: time.strftime("%H:%M:%S", time.localtime(ts))

    trades = [x for x in lib.read_jsonl(d / "trades.jsonl")
              if x.get("detected_at", 0) > cutoff and not x.get("backfill")]
    execs = [x for x in lib.read_jsonl(d / "executions_mainnet.jsonl") if x.get("ts", 0) > cutoff]
    fills = [x for x in lib.read_jsonl(d / "sleeve_fills.jsonl") if x.get("quoted_at", 0) > cutoff]

    by_sig = {}
    for e in execs:
        by_sig.setdefault(e.get("signal_tx"), []).append(e)
    for f in fills:
        by_sig.setdefault(f.get("tx_hash"), []).append(f)

    print(f"\n===== {t(cutoff)} → {t(now)}  |  AJC vs vault =====")
    if not trades:
        print("  AJC: no activity detected")
    for x in trades:
        a = x["asset_token"]
        line = (f"  {t(x['detected_at'])} [{x.get('chain', 'robinhood'):9}] "
                f"{x['kind']:8} {x['side']:4} {a['symbol']:10} ${(x.get('usd_value') or 0):>9,.0f}")
        responses = by_sig.get(x["tx_hash"], [])
        if x["kind"] != "swap":
            print(line + "  -> not a trade (never mirrored)")
            continue
        if not responses:
            print(line + "  -> no vault decision recorded yet")
            continue
        for r in responses:
            if r.get("kind") == "skip":
                print(line + f"  -> SKIP: {r['reason']}")
            elif "vault_tx" in r:
                print(line + f"  -> EXECUTED ${r.get('usd', 0):,.2f} ({r['vault_tx'][:12]}...)")
            elif "sleeve_sig" in r or "executed" in r:
                if r.get("executed"):
                    print(line + f"  -> SLEEVE EXECUTED ${r.get('exec_usd', r.get('usd', 0)):,.2f}"
                                 f" ({str(r.get('sleeve_sig'))[:12]}...)")
                else:
                    print(line + f"  -> sleeve skip: {r.get('skip_reason', 'paper only')}")

    ops = [e for e in execs if e.get("kind") in ("bridge", "bridge_back", "rotation_bridge")]
    refills = [x for x in lib.read_jsonl(d / "gas_refills.jsonl") if x.get("ts", 0) > cutoff]
    if ops or refills:
        print("  --- treasury/ops ---")
        for e in ops:
            print(f"  {t(e['ts'])} {e['kind']}: ${e.get('usd', 0):,.2f}")
        for e in refills:
            print(f"  {t(e['ts'])} {e['kind']}: ${e.get('usd', 0):,.2f}")

    navf = d / "nav_mainnet.json"
    if navf.exists():
        nav = json.loads(navf.read_text())
        age = (now - nav["ts"]) / 60
        stale = "  (STALE — is the executor running?)" if age > 15 else ""
        print(f"  NAV ${nav['nav_usd']:,.2f} (posted {age:.0f}m ago){stale}")


def main():
    cfg = lib.load_config()
    window = WINDOW
    if "--window" in sys.argv:
        window = int(sys.argv[sys.argv.index("--window") + 1])
    if "--loop" in sys.argv:
        while True:
            try:
                one_report(cfg, window)
            except Exception as e:
                print(f"  [report error] {e}")
            time.sleep(window)
    else:
        one_report(cfg, window)


if __name__ == "__main__":
    main()
