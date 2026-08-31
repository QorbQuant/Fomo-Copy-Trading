# Supervised VM deployment

Moves the three keeper processes (EVM watcher, Solana watcher, mainnet
executor) from the laptop to a VM under systemd: auto-restart on crash,
survives reboots (`enable`), logs in journald.

## 1. Provision

Any small Ubuntu 24.04 VM: 1 vCPU / 1 GB is plenty (Hetzner CX22, DO basic
droplet, AWS Lightsail). Requirements: root SSH from the Mac
(`ssh root@IP` must work), outbound internet.

## 2. Migrate (one command, from the Mac)

```
bash ~/Documents/fomo-copy-vault/deploy/migrate.sh root@YOUR.VM.IP
```

This stops every local process FIRST (two concurrent keepers = nonce races
and double trades), rsyncs the repo **including `.env` secrets and `data/`
state**, installs python deps + foundry, and starts the three services.

## 3. Operate

```
ssh root@IP journalctl -u fomo-executor -f        # live log
ssh root@IP systemctl status 'fomo-*'             # health
ssh root@IP systemctl restart fomo-executor       # bounce one service
```

Manual sync (always pause the executor around it):

```
ssh root@IP 'systemctl stop fomo-executor && cd /home/fomo/fomo-copy-vault \
  && sudo -u fomo python3 sync.py --mainnet && systemctl start fomo-executor'
```

## Notes

- `SLEEVE_EXECUTE=1` is set in the solana-watcher and executor units: live
  Solana signals execute for real on the VM. Delete that line from the unit
  and `systemctl daemon-reload && systemctl restart ...` to go paper-only.
- The laptop copy of the repo becomes a dev checkout only. Never run
  watchers/executor/sync locally while the VM services are up. To update the
  VM after code changes: rerun migrate.sh (it re-stops local strays, syncs,
  restarts services via setup.sh).
- Secrets live in `/home/fomo/fomo-copy-vault/.env` and `contracts/.env` on
  the VM — same plaintext posture as the laptop. KMS/HSM remains the
  before-outside-money upgrade.
- Not yet covered: hang detection (a process stuck on a dead RPC without
  crashing). systemd restarts crashes only. Watchdog heartbeats are the next
  hardening step if it ever bites.
