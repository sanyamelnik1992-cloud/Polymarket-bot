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
for u in pmbot-5m pmbot-5m-b pmbot-5m-c pmbot-5m-d pmbot-5m-e pmbot-5m-f pmbot-5m-g pmbot-5m-g2 pmbot-5m-h pmbot-5m-live pmbot-lock pmbot-lock-wide pmbot-lock-15 pmbot-lock-maker pmbot-lock-maker2 pmbot-lock-tier pmbot-lock-tier-wide pmbot-lock-15-tier pmbot-lock-15-alt pmbot-lock-maker3 pmbot-lock-99 pmbot-lock-99-live pmbot-lock-tier-pb pmbot-lock-tier-late pmbot-lock-live pmbot-lock-15-live pmbot-lock-early pmbot-lock-15-early pmbot-lock-15-early-live pmbot-lock-fair pmbot-lock-fair-live pmbot-lock-fair-fut pmbot-lock-15-fair pmbot-rewards pmbot-rewards-live pmbot-weather-rec pmbot-arb pmbot-hourly pmbot-hourly-last pmbot-recorder pmbot-tg pmbot-autoupdate.service; do
  for a in start restart stop; do CMDS="$CMDS, /usr/bin/systemctl $a --no-block $u"; done
done
echo "$USER_NAME ALL=(root) NOPASSWD: ${CMDS#, }" > /etc/sudoers.d/pmbot
chmod 440 /etc/sudoers.d/pmbot
visudo -cf /etc/sudoers.d/pmbot >/dev/null
usermod -aG systemd-journal "$USER_NAME" 2>/dev/null || true

systemctl daemon-reload

# Retired bots (reports/83 section 15): never started at boot or by /resume. On 22 Nov a /resume after the live interceptor's
# day stop started the old makers A and C, L2 and the 5m early bot, still enabled at boot, next to the live bots. A paper unit
# of this list is also stopped; a live one only disabled. Bring one back by hand: /restart <unit> (it stays off at boot).
RETIRED="pmbot-5m pmbot-5m-b pmbot-5m-c pmbot-5m-d pmbot-5m-e pmbot-5m-f pmbot-5m-g pmbot-5m-g2 pmbot-5m-h pmbot-5m-live pmbot-lock pmbot-lock-wide pmbot-lock-15 pmbot-lock-maker pmbot-lock-maker2 pmbot-lock-tier pmbot-lock-tier-wide pmbot-lock-15-tier pmbot-lock-15-alt pmbot-lock-maker3 pmbot-lock-99 pmbot-lock-tier-pb pmbot-lock-tier-late pmbot-lock-live pmbot-lock-early pmbot-lock-15-early-live pmbot-lock-fair-fut pmbot-hourly pmbot-hourly-last pmbot-arb pmbot-weather-rec pmbot-rewards pmbot-rewards-live"
for u in $RETIRED; do
  f="$REPO/deploy/$u.service"
  [ -e "$f" ] || continue
  if grep -q -- '--live' "$f"; then
    systemctl disable -q "$u" 2>/dev/null || true
  else
    systemctl --no-block disable -q --now "$u" 2>/dev/null || true     # no wait: a bot's graceful stop can take minutes
  fi
done
