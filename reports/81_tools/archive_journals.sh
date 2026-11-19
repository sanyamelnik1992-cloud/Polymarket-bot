#!/usr/bin/env bash
# Journals of the bots that no longer run (stopped on 17 Nov or long before): one tar.gz in /root/archive, verified, then
# the raw directories are removed. Live journals (lock-live, lock-15-live, lock-99-live) stay.
set -euo pipefail
D=/home/pmbot/Polymarket-bot/data
A=/root/archive/journals-stopped-20261117.tar.gz
DIRS="journal-colo journal-colo-strict journal-colo-v2 journal-colo-v2-strict journal-g journal-g2 journal-h journal-hourly-last journal-live journal-live-twin journal-live-v3 journal-lock journal-lock-15 journal-lock-15-alt journal-lock-15-tier journal-lock-99 journal-lock-maker journal-lock-maker2 journal-lock-maker3 journal-lock-tier journal-lock-tier-pb journal-lock-tier-wide journal-lock-wide journal-rewards journal-rewards-live journal-rewards-probe journal-v3"
mkdir -p /root/archive
cd "$D"
EXIST=""; for d in $DIRS; do [ -d "$d" ] && EXIST="$EXIST $d"; done
nice -n 19 ionice -c 3 tar czf "$A.tmp" $EXIST
n_files=$(find $EXIST -type f | wc -l)
n_tar=$(tar tzf "$A.tmp" | grep -vc "/$")
gzip -t "$A.tmp"
if [ "$n_files" = "$n_tar" ]; then
  mv "$A.tmp" "$A"; rm -rf $EXIST
  echo "archived $n_files files into $A ($(du -sh "$A" | cut -f1)); removed:$EXIST"
else
  echo "MISMATCH files=$n_files tar=$n_tar: nothing removed"; exit 1
fi
