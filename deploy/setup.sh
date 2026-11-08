#!/usr/bin/env bash
# One-time server setup (Ubuntu/Debian), safe to re-run. Run from the repo root:  sudo bash deploy/setup.sh
# (a fresh server: use deploy/bootstrap.sh, it creates the pmbot user, clones the repo and calls this script)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$(stat -c %U "$REPO")}"

apt-get -o DPkg::Lock::Timeout=900 update -y
apt-get -o DPkg::Lock::Timeout=900 install -y python3 python3-venv python3-pip chrony git
systemctl enable --now chrony            # accurate clock matters for 5-minute windows

# small VPS: add swap so pip install and two bots never hit the OOM killer
if [ "$(swapon --noheadings | wc -l)" = "0" ] && [ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -lt 4000000 ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

PYBIN=python3
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then   # e.g. Ubuntu 22.04 ships 3.10
  if ! apt-cache show python3.12-venv >/dev/null 2>&1; then
    apt-get -o DPkg::Lock::Timeout=900 install -y software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get -o DPkg::Lock::Timeout=900 update -y
  fi
  apt-get -o DPkg::Lock::Timeout=900 install -y python3.12 python3.12-venv && PYBIN=python3.12
fi
[ -x "$REPO/.venv/bin/python" ] || sudo -u "$USER_NAME" "$PYBIN" -m venv "$REPO/.venv"
sudo -u "$USER_NAME" "$REPO/.venv/bin/pip" install -q --upgrade pip
sudo -u "$USER_NAME" "$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt" polymarket-client==0.16.0

mkdir -p /etc/pmbot
if [ ! -f /etc/pmbot/pmbot.env ]; then
  cp "$REPO/deploy/pmbot.env.example" /etc/pmbot/pmbot.env
fi
chmod 600 /etc/pmbot/pmbot.env
chown "$USER_NAME" /etc/pmbot/pmbot.env
sudo -u "$USER_NAME" mkdir -p "$REPO/data/state"

bash "$REPO/deploy/install_units.sh" "$USER_NAME"
systemctl enable --now pmbot-autoupdate.timer pmbot-cleanup.timer pmbot-wallet.timer
systemctl enable pmbot-reports.timer      # started once the deploy key can push (see RUNBOOK)

echo
echo "Done. Next:"
echo "  1) $REPO/.venv/bin/python $REPO/scripts/preflight.py        # must show 0 failed"
echo "  2) $REPO/.venv/bin/python $REPO/scripts/latency_probe.py 120  # CLOB REST RTT should be < 15 ms"
echo "  3) systemctl enable --now pmbot-5m pmbot-tg                  # PAPER mode; add pmbot-5m-b / pmbot-recorder if RAM/disk allow"
echo "  4) journalctl -u pmbot-5m -f                                # live log"
echo "  Live trading: see RUNBOOK.md, edit /etc/pmbot/pmbot.env and the *_live configs first."
