#!/usr/bin/env bash
# Code-only update to a running VM. UNLIKE migrate.sh, this NEVER touches the
# VM's live data/ or secrets — it syncs code + config, reinstalls unit files,
# and restarts services. Use this for every post-migration code change.
#
#   bash deploy/update.sh root@YOUR.VM.IP
set -euo pipefail
HOST=${1:?usage: update.sh root@host}
SRC="$HOME/Documents/fomo-copy-vault"

echo "== syncing code + config (preserving VM data/ and .env) =="
rsync -az \
  --exclude 'data' --exclude '.env' --exclude 'contracts/.env' \
  --exclude 'contracts/cache' --exclude 'contracts/out' \
  --exclude 'contracts/broadcast' --exclude '__pycache__' --exclude '.git' \
  "$SRC/" "$HOST:/home/fomo/fomo-copy-vault/"

echo "== reinstalling unit files + restarting services =="
ssh "$HOST" 'bash -s' <<'REMOTE'
set -e
chown -R fomo:fomo /home/fomo/fomo-copy-vault
install -m 644 /home/fomo/fomo-copy-vault/deploy/fomo-*.service \
  /home/fomo/fomo-copy-vault/deploy/fomo-*.timer /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload
systemctl restart fomo-watcher fomo-solana-watcher fomo-executor
echo "core services restarted"
REMOTE
echo "== update complete =="
