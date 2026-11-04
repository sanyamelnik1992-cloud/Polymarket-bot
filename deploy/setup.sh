#!/usr/bin/env bash
# One-time server setup (Ubuntu/Debian). Run from the repo root:  sudo bash deploy/setup.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$(whoami)}"

apt-get update -y
apt-get install -y python3 python3-venv python3-pip chrony git
systemctl enable --now chrony            # accurate clock matters for 5-minute windows

sudo -u "$USER_NAME" python3 -m venv "$REPO/.venv"
sudo -u "$USER_NAME" "$REPO/.venv/bin/pip" install --upgrade pip
sudo -u "$USER_NAME" "$REPO/.venv/bin/pip" install -r "$REPO/requirements.txt" polymarket-client==0.16.0

mkdir -p /etc/pmbot
if [ ! -f /etc/pmbot/pmbot.env ]; then
  cp "$REPO/deploy/pmbot.env.example" /etc/pmbot/pmbot.env
  chmod 600 /etc/pmbot/pmbot.env
  chown "$USER_NAME" /etc/pmbot/pmbot.env
fi
sudo -u "$USER_NAME" mkdir -p "$REPO/data"

for unit in pmbot-5m pmbot-hourly pmbot-recorder; do
  sed -e "s#__REPO__#$REPO#g" -e "s#__USER__#$USER_NAME#g" "$REPO/deploy/$unit.service" > "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload

echo
echo "Done. Next:"
echo "  1) $REPO/.venv/bin/python $REPO/scripts/preflight.py        # must show 0 failed"
echo "  2) systemctl start pmbot-5m pmbot-hourly                    # PAPER mode by default"
echo "  3) journalctl -u pmbot-5m -f                                # live log"
echo "  Live trading: see RUNBOOK.md, edit /etc/pmbot/pmbot.env and the *_live configs first."
