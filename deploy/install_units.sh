#!/usr/bin/env bash
# Render deploy/*.service and deploy/*.timer into /etc/systemd/system (replacing __REPO__ / __USER__) and reload.
# Run as root from anywhere:  bash deploy/install_units.sh [user]
# Called by deploy/setup.sh and by deploy/autoupdate.sh when files in deploy/ change.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${1:-$(stat -c %U "$REPO")}"

for f in "$REPO"/deploy/*.service "$REPO"/deploy/*.timer; do
  [ -e "$f" ] || continue
  sed -e "s#__REPO__#$REPO#g" -e "s#__USER__#$USER_NAME#g" "$f" > "/etc/systemd/system/$(basename "$f")"
done

# the Telegram bot may start/stop/restart exactly these units and trigger an update -- nothing else
CMDS=""
for u in pmbot-5m pmbot-5m-b pmbot-5m-c pmbot-5m-d pmbot-5m-e pmbot-5m-f pmbot-5m-g pmbot-5m-g2 pmbot-5m-h pmbot-5m-live pmbot-lock pmbot-lock-wide pmbot-lock-15 pmbot-lock-maker pmbot-lock-maker2 pmbot-lock-tier pmbot-lock-tier-wide pmbot-lock-15-tier pmbot-lock-maker3 pmbot-lock-99 pmbot-lock-99-live pmbot-lock-tier-pb pmbot-lock-tier-late pmbot-lock-live pmbot-lock-15-live pmbot-rewards pmbot-arb pmbot-hourly pmbot-recorder pmbot-tg pmbot-autoupdate.service; do
  for a in start restart stop; do CMDS="$CMDS, /usr/bin/systemctl $a --no-block $u"; done
done
echo "$USER_NAME ALL=(root) NOPASSWD: ${CMDS#, }" > /etc/sudoers.d/pmbot
chmod 440 /etc/sudoers.d/pmbot
visudo -cf /etc/sudoers.d/pmbot >/dev/null
usermod -aG systemd-journal "$USER_NAME" 2>/dev/null || true

systemctl daemon-reload
