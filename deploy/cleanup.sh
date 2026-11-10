#!/usr/bin/env bash
# Hourly disk housekeeping (pmbot-cleanup.timer). The recorder writes 6-13 GB a day, small VPS disks fill fast.
#   PMBOT_REC_KEEP_HOURS  a recording directory data/rec/* is deleted once nothing in it was written for this
#                         many hours (default 12; the directory being recorded right now is never deleted)
#   PMBOT_REC_ROLL_GB     the recorder is restarted (new directory) when its clob.jsonl passes this size (default 4)
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
# The book recording (clob.jsonl) is 90 % of the volume and gzips ~8x. Roll the recorder once its file passes
# PMBOT_REC_ROLL_GB (default 4: a restart opens a new directory, the gap is a few seconds between closing minutes at
# worst) and gzip every finished clob.jsonl at the lowest priority. reports/51_tools/extract_day.py reads .gz.
# On 10 Nov one 13 GB file filled the 30 GB disk to 91 % in 21 hours and the rule below was about to stop the recorder.
ROLL_GB="${PMBOT_REC_ROLL_GB:-4}"
active="$(ls -d "$REPO"/data/rec/*/ 2>/dev/null | tail -1)"
if [ -n "$active" ] && [ -f "${active}clob.jsonl" ] && systemctl is-active -q pmbot-recorder; then
  size_gb=$(( $(stat -c %s "${active}clob.jsonl") / 1073741824 ))
  if [ "$size_gb" -ge "$ROLL_GB" ]; then
    echo "rolling the recorder: ${active}clob.jsonl is ${size_gb} GB"; systemctl restart pmbot-recorder; sleep 5
    active="$(ls -d "$REPO"/data/rec/*/ 2>/dev/null | tail -1)"
  fi
fi
for f in "$REPO"/data/rec/*/clob.jsonl; do
  [ -f "$f" ] || continue
  [ "$(dirname "$f")/" = "$active" ] && systemctl is-active -q pmbot-recorder && continue
  if [ -z "$(find "$f" -mmin -5 -print -quit)" ] && ! pgrep -f "gzip.*$f" > /dev/null; then
    echo "gzip $f"; nice -n 19 ionice -c 3 gzip "$f" && chown "$OWNER": "$f.gz" || true
  fi
done
free_gb=$(( $(df --output=avail -B1G "$REPO" | tail -1) ))
if [ "$free_gb" -lt "$MIN_FREE" ] && systemctl is-active -q pmbot-recorder; then
  systemctl stop pmbot-recorder
  sudo -u "$OWNER" -H "$PY" "$REPO/scripts/tg_send.py" "⚠️ свободно ${free_gb} ГБ: рекордер остановлен, боты работают" || true
fi
