#!/usr/bin/env bash
# Provisioning guard: replicas share the authorized 14:00 KST boundary.
# Runs before model setup so failed/abandoned provisioning still powers off.
set -euo pipefail
sudo -n systemd-run --unit=zoro-replica-stop \
  --on-calendar='2026-11-20 05:00:00 UTC' /usr/sbin/shutdown -h now
