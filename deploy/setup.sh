#!/usr/bin/env bash
# Server-side bootstrap. Run as root on Ubuntu 24.04 AFTER the repo has been
# rsynced to /home/fomo/fomo-copy-vault (migrate.sh does both).
set -euo pipefail

apt-get update -y
apt-get install -y python3 python3-pip curl git

id -u fomo &>/dev/null || useradd -m -s /bin/bash fomo
chown -R fomo:fomo /home/fomo/fomo-copy-vault

# foundry (cast/forge — the executor shells out to cast for all EVM txs)
if [ ! -x /home/fomo/.foundry/bin/cast ]; then
  sudo -u fomo bash -c 'curl -sL https://foundry.paradigm.xyz | bash'
  sudo -u fomo bash -c '/home/fomo/.foundry/bin/foundryup'
fi

sudo -u fomo python3 -m pip install --break-system-packages --quiet requests solders base58

install -m 644 /home/fomo/fomo-copy-vault/deploy/fomo-*.service /home/fomo/fomo-copy-vault/deploy/fomo-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now fomo-watcher fomo-solana-watcher fomo-executor fomo-report.timer
# multi-chain observation (paper only): watch AJC's same EVM address on each chain
systemctl enable --now fomo-observe@base fomo-observe@monad fomo-observe@bnb fomo-observe@ethereum

sleep 5
systemctl --no-pager --lines=5 status fomo-watcher fomo-solana-watcher fomo-executor || true
echo
echo "== deployed. logs: journalctl -u fomo-executor -f =="
