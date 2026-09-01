"""Off-box state backup: snapshot the executor's state files and ship them off
the VM so a box loss doesn't lose in-flight bridge tracking, the token map, or
the deployment record. Keeps a local rotating copy too (for corruption
recovery), and posts the tarball to BACKUP_WEBHOOK (or ALERT_WEBHOOK) — Discord
takes file attachments, so this needs no extra infra.

State files carry NO secrets: private keys live in .env, which is never read
here. Only data/*.json[l] + contracts/deployments.json are captured.

    python3 backup.py
"""

import io
import json
import tarfile
import time
from pathlib import Path

ROOT = Path("/home/fomo/fomo-copy-vault")
DATA = ROOT / "data"
LOCAL = ROOT / "backups"
KEEP = 14  # local rotating snapshots to retain


def _env(key):
    try:
        for line in (ROOT / "contracts" / ".env").read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _make_tar():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(DATA.glob("*.json")) + sorted(DATA.glob("*.jsonl")):
            tar.add(p, arcname=p.name)
        dep = ROOT / "contracts" / "deployments.json"
        if dep.exists():
            tar.add(dep, arcname="deployments.json")
    return buf.getvalue()


def _rotate(stamp, blob):
    LOCAL.mkdir(exist_ok=True)
    (LOCAL / f"state-{stamp}.tar.gz").write_bytes(blob)
    for old in sorted(LOCAL.glob("state-*.tar.gz"))[:-KEEP]:
        old.unlink()


def _ship(stamp, blob):
    url = _env("BACKUP_WEBHOOK") or _env("ALERT_WEBHOOK")
    if not url:
        print("[backup] no BACKUP_WEBHOOK/ALERT_WEBHOOK set — local snapshot only")
        return
    try:
        import requests
        if "discord" in url:
            requests.post(
                url,
                data={"payload_json": json.dumps({"content": f"🗄️ state backup {stamp}"})},
                files={"file": (f"state-{stamp}.tar.gz", blob)},
                timeout=30,
            )
            print(f"[backup] shipped state-{stamp}.tar.gz off-box ({len(blob)} bytes)")
        else:
            # Slack/generic incoming webhooks can't take file attachments
            requests.post(url, json={"text": f"state backup {stamp} saved locally "
                                             f"({len(blob)} bytes) — this webhook can't attach files"},
                          timeout=15)
    except Exception as e:
        print(f"[backup] off-box ship failed (local snapshot still kept): {str(e)[:120]}")


def main():
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    blob = _make_tar()
    _rotate(stamp, blob)
    _ship(stamp, blob)


if __name__ == "__main__":
    main()
