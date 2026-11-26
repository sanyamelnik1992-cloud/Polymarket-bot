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
for u in pmbot-5m pmbot-5m-b pmbot-5m-c pmbot-5m-d pmbot-5m-e pmbot-5m-f pmbot-5m-g pmbot-5m-g2 pmbot-5m-h pmbot-5m-live pmbot-lock pmbot-lock-wide pmbot-lock-15 pmbot-lock-maker pmbot-lock-maker2 pmbot-lock-tier pmbot-lock-tier-wide pmbot-lock-15-tier pmbot-lock-15-alt pmbot-lock-maker3 pmbot-lock-99 pmbot-lock-99-live pmbot-lock-tier-pb pmbot-lock-tier-late pmbot-lock-live pmbot-lock-15-live pmbot-lock-early pmbot-lock-15-early pmbot-lock-15-early-live pmbot-lock-fair pmbot-lock-fair-live pmbot-lock-fair-fut pmbot-lock-15-fair pmbot-lock-99-sigma pmbot-maker pmbot-maker-safe pmbot-maker-v2 pmbot-maker-live pmbot-rewards pmbot-rewards-live pmbot-weather-rec pmbot-feedrec pmbot-arb pmbot-hourly pmbot-hourly-last pmbot-recorder pmbot-tg pmbot-autoupdate.service; do
  for a in start restart stop; do CMDS="$CMDS, /usr/bin/systemctl $a --no-block $u"; done
done
echo "$USER_NAME ALL=(root) NOPASSWD: ${CMDS#, }" > /etc/sudoers.d/pmbot
chmod 440 /etc/sudoers.d/pmbot
visudo -cf /etc/sudoers.d/pmbot >/dev/null
usermod -aG systemd-journal "$USER_NAME" 2>/dev/null || true

systemctl daemon-reload

# Retired bots (reports/83 section 15): never started at boot or by /resume. On 22 Nov a /resume after the live interceptor's
# day stop started the old makers A and C, L2 and the 5m early bot, still enabled at boot, next to the live bots. A paper unit
# of this list is also stopped; a live one only disabled, unless it is in STOP_LIVE. Bring one back by hand: /restart <unit>
# (it stays off at boot).
RETIRED="pmbot-5m pmbot-5m-b pmbot-5m-c pmbot-5m-d pmbot-5m-e pmbot-5m-f pmbot-5m-g pmbot-5m-g2 pmbot-5m-h pmbot-5m-live pmbot-lock pmbot-lock-wide pmbot-lock-15 pmbot-lock-maker pmbot-lock-maker2 pmbot-lock-tier pmbot-lock-tier-wide pmbot-lock-15-tier pmbot-lock-15-alt pmbot-lock-maker3 pmbot-lock-99 pmbot-lock-tier-pb pmbot-lock-tier-late pmbot-lock-live pmbot-lock-early pmbot-lock-fair-fut pmbot-hourly pmbot-hourly-last pmbot-arb pmbot-weather-rec pmbot-rewards pmbot-rewards-live pmbot-lock-fair pmbot-lock-fair-live pmbot-lock-15-fair pmbot-maker pmbot-maker-safe"
# Live units of RETIRED that are stopped too (a graceful stop: open windows settle first). reports/86 (23 Nov): the live
# interceptor lost on the fills it can actually get (33 fills, 6 won where the prices paid implied 8.3). 24 Nov (owner: "выключай
# перехватчика"): its paper twin pmbot-lock-fair is retired too (reports/87 section 7: no price source beats Binance).
# 26 Nov (reports/94): the paper maker pmbot-maker ("fair" reference) makes room on the 2 vCPU for the live maker; the
# live test runs the rule of reports/95, whose paper twin is pmbot-maker-v2 (the old symmetric "min" rule, pmbot-maker-safe, is retired
# too: two paper makers + the live one do not fit the 2 vCPU; its paper result and the tape simulation stay in reports/94 and 95).
STOP_LIVE="pmbot-lock-fair-live"
for u in $RETIRED; do
  f="$REPO/deploy/$u.service"
  [ -e "$f" ] || continue
  if grep -q -- '--live' "$f" && ! grep -qw -- "$u" <<< "$STOP_LIVE"; then
    systemctl disable -q "$u" 2>/dev/null || true
  else
    systemctl --no-block disable -q --now "$u" 2>/dev/null || true     # no wait: a bot's graceful stop can take minutes
  fi
done

# New research / paper units are started here ONCE, on the first deploy that brings them (the owner cannot run /restart; only
# his brother can): a marker in data/state remembers it, so a unit stopped later with /off stays stopped. reports/87: the
# price-source race recorder (stops by itself at its config's `until`); reports/88: the Sniper with the volatility pause (paper);
# reports/89: the paper maker (bids on both sides of the BTC 5m windows). reports/92 (24 Nov, the owner's "Давай"): the LIVE
# 15m early sniper at 5 shares is started here once too; after its day stop it stays stopped until a human restarts it.
# reports/94 (26 Nov, the owner's "запускай"): the LIVE maker test (5 shares, $10 resting, -$5 day stop) is started once too.
START_ONCE="pmbot-feedrec pmbot-lock-99-sigma pmbot-maker-v2 pmbot-lock-15-early-live pmbot-maker-live"
for u in $START_ONCE; do
  [ -e "$REPO/deploy/$u.service" ] || continue
  mark="$REPO/data/state/.started-$u"
  [ -e "$mark" ] && continue
  mkdir -p "$REPO/data/state"
  systemctl is-active -q "$u" || systemctl --no-block start "$u" 2>/dev/null || true
  touch "$mark"
done
