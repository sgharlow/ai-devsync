#!/usr/bin/env bash
# Installs devsync automation on a Linux machine (the counterpart of
# install-devsync-task.ps1). Three triggers, all calling scripts/devsync-sync.sh:
#
#   devsync-boot-shutdown.service  sync once the network is up at boot, and again at
#                                  shutdown/reboot BEFORE the network goes down
#   devsync-sync.timer             every 4h (the Windows task's cadence), so a machine
#                                  left running for days still converges
#
# System units with User=<you>, not `systemctl --user`: user units only run while you
# are logged in (or with linger), and a boot/shutdown hook must not depend on a login.
#
# Usage:  scripts/install-devsync-systemd.sh            install / re-install
#         scripts/install-devsync-systemd.sh --uninstall

set -euo pipefail

RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WRAPPER="$REPO/scripts/devsync-sync.sh"
UNIT_DIR=/etc/systemd/system

if [ "${1:-}" = "--uninstall" ]; then
  sudo systemctl disable --now devsync-sync.timer devsync-boot-shutdown.service 2>/dev/null || true
  sudo rm -f "$UNIT_DIR"/devsync-boot-shutdown.service "$UNIT_DIR"/devsync-sync.service "$UNIT_DIR"/devsync-sync.timer
  sudo systemctl daemon-reload
  echo "devsync units removed"
  exit 0
fi

[ -f "$RUN_HOME/.devsync/machine.json" ] || { echo "not adopted yet: $RUN_HOME/.devsync/machine.json missing" >&2; exit 1; }
chmod +x "$WRAPPER"

sudo tee "$UNIT_DIR/devsync-boot-shutdown.service" >/dev/null <<EOF
[Unit]
Description=devsync: sync at boot and at shutdown
Wants=network-online.target
# Stop order is the reverse of start order, so ExecStop at shutdown runs while the
# network and DNS are still up.
After=network-online.target nss-lookup.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=$RUN_USER
# '-': a failed boot sync (offline, auth) must not leave the unit failed, or systemd
# skips ExecStop and the shutdown sync never runs. The wrapper's log records it.
ExecStart=-$WRAPPER boot
ExecStop=-$WRAPPER shutdown
TimeoutStartSec=300
# A routine sync is 30-60s; this caps how long a hung remote can delay poweroff.
TimeoutStopSec=180

[Install]
WantedBy=multi-user.target
EOF

sudo tee "$UNIT_DIR/devsync-sync.service" >/dev/null <<EOF
[Unit]
Description=devsync: scheduled sync
Wants=network-online.target
After=network-online.target nss-lookup.target

[Service]
Type=oneshot
User=$RUN_USER
ExecStart=$WRAPPER timer
# Exit 2 = conflicts written: the sync worked, a human has work to do.
SuccessExitStatus=2
TimeoutStartSec=600
EOF

sudo tee "$UNIT_DIR/devsync-sync.timer" >/dev/null <<EOF
[Unit]
Description=devsync: sync every 4 hours

[Timer]
OnCalendar=*-*-* 00/4:00:00
RandomizedDelaySec=10min
# No catch-up run: the boot sync already covers a machine that was off.
Persistent=false

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
# enable without --now for the boot unit: starting it here would run ExecStart
# immediately, which is fine, but make it explicit so the first run is visible.
sudo systemctl enable devsync-boot-shutdown.service devsync-sync.timer
sudo systemctl start devsync-boot-shutdown.service devsync-sync.timer
echo "installed for $RUN_USER; log: $RUN_HOME/.devsync/sync.log"
systemctl list-timers devsync-sync.timer --no-pager | head -3
