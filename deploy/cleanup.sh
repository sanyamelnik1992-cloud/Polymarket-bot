#!/usr/bin/env bash
# Hourly disk housekeeping (pmbot-cleanup.timer). The recorder writes 6-13 GB a day, small VPS disks fill fast.
#   PMBOT_REC_KEEP_HOURS  a recording directory data/rec/* is deleted once nothing in it was written for this
#                         many hours (default 12; the directory being recorded right now is never deleted)
#   PMBOT_MIN_FREE_GB     if free space is still below this, the recorder is stopped (default 3); bots keep running
# Journals (data/journal*) are small and are never deleted here.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
KEEP_H="${PMBOT_REC_KEEP_HOURS:-12}"
MIN_FREE="${PMBOT_MIN_FREE_GB:-3}"
PY="$REPO/.venv/bin/python"
OWNER="$(stat -c %U "$REPO")"

for d in "$REPO"/data/rec/*/; do
  [ -d "$d" ] || continue
  if [ -z "$(find "$d" -type f -mmin -"$((KEEP_H * 60))" -print -quit)" ]; then
    echo "deleting old recording $d"; rm -rf "$d"
  fi
done
free_gb=$(( $(df --output=avail -B1G "$REPO" | tail -1) ))
if [ "$free_gb" -lt "$MIN_FREE" ] && systemctl is-active -q pmbot-recorder; then
  systemctl stop pmbot-recorder
  sudo -u "$OWNER" -H "$PY" "$REPO/scripts/tg_send.py" "⚠️ свободно ${free_gb} ГБ: рекордер остановлен, боты работают" || true
fi
