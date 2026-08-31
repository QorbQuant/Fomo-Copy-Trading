#!/usr/bin/env bash
# Run on the Mac: moves the whole system to a supervised VM in one shot.
#   bash deploy/migrate.sh root@YOUR.VM.IP
#
# Order matters: local processes are stopped BEFORE the VM starts, so two
# keepers never run at once (nonce races, double trades).
set -euo pipefail
HOST=${1:?usage: migrate.sh root@host}
SRC="$HOME/Documents/fomo-copy-vault"

echo "== 1/4 stopping ALL local processes (watchers + executor) =="
pkill -f "fomo-copy-vault/watcher.py" 2>/dev/null || true
pkill -f "fomo-copy-vault/solana_watcher.py" 2>/dev/null || true
pkill -f "fomo-copy-vault/executor.py" 2>/dev/null || true
sleep 2

echo "== 2/4 preparing server user =="
ssh "$HOST" 'id -u fomo &>/dev/null || useradd -m -s /bin/bash fomo'

echo "== 3/4 syncing repo + state + secrets (.env files included) =="
rsync -az --delete \
  --exclude 'contracts/cache' --exclude 'contracts/out' \
  --exclude 'contracts/broadcast' --exclude '__pycache__' \
  --exclude '.git' \
  "$SRC/" "$HOST:/home/fomo/fomo-copy-vault/"

echo "== 4/4 bootstrapping + starting services =="
ssh "$HOST" 'bash /home/fomo/fomo-copy-vault/deploy/setup.sh'

echo
echo "== migration complete. useful commands: =="
echo "  ssh $HOST journalctl -u fomo-executor -f      # live executor log"
echo "  ssh $HOST systemctl status 'fomo-*'           # all three services"
echo "  ssh $HOST 'cd /home/fomo/fomo-copy-vault && sudo -u fomo python3 gas.py --status'"
echo
echo "Local processes are STOPPED and must stay stopped. To run a manual sync"
echo "later, do it ON THE SERVER with the executor paused:"
echo "  ssh $HOST 'systemctl stop fomo-executor && cd /home/fomo/fomo-copy-vault && sudo -u fomo python3 sync.py --mainnet && systemctl start fomo-executor'"
