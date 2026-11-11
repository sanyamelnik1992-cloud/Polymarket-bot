#!/usr/bin/env bash
# Pull new commits from GitHub, test them, restart what changed.  Runs as root from pmbot-autoupdate.timer
# (every 2 min) or on demand: /update in Telegram, or  sudo systemctl start pmbot-autoupdate
#
#   branch: PMBOT_BRANCH in /etc/pmbot/pmbot.env (default: main)
#   fast-forward only; if the tests fail the update is rolled back, the commit is remembered as bad and skipped
#   until a newer commit arrives. Never touches data/, the env file or untracked files.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="$(stat -c %U "$REPO")"
BRANCH="${PMBOT_BRANCH:-main}"
STATE="$REPO/data/state"
BAD="$STATE/autoupdate_bad_commit"
PY="$REPO/.venv/bin/python"
cd "$REPO"

as_owner() { sudo -u "$OWNER" -H "$@"; }
notify() { as_owner "$PY" scripts/tg_send.py "$1" || true; echo "$1"; }
g() { as_owner git "$@"; }   # git as the repo owner (root would hit "dubious ownership")

as_owner mkdir -p "$STATE"
g fetch -q origin "$BRANCH" || { echo "git fetch failed"; exit 1; }
OLD="$(g rev-parse HEAD)"
NEW="$(g rev-parse "origin/$BRANCH")"
[ "$OLD" = "$NEW" ] && exit 0
[ -f "$BAD" ] && [ "$(cat "$BAD")" = "$NEW" ] && exit 0

if [ "$(g rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]; then
  g checkout -q "$BRANCH" 2>/dev/null || g checkout -q -b "$BRANCH" "origin/$BRANCH" || {
    notify "⚠️ автообновление: не могу переключиться на ветку $BRANCH"; exit 1; }
  OLD="$(g rev-parse HEAD)"
fi
if [ -n "$(g status --porcelain --untracked-files=no)" ]; then
  notify "⚠️ автообновление пропущено: на сервере есть незакоммиченные правки в коде (git status)"
  exit 1
fi
if ! g merge -q --ff-only "origin/$BRANCH"; then
  notify "⚠️ автообновление: история $BRANCH разошлась с сервером (не fast-forward). Нужна ручная проверка."
  exit 1
fi

CHANGED="$(g diff --name-only "$OLD" "$NEW")"
LOG="$(g log --format='• %h %s' "$OLD..$NEW" | head -15)"

if echo "$CHANGED" | grep -q '^requirements.txt$'; then
  as_owner "$REPO/.venv/bin/pip" install -q -r requirements.txt || {
    g reset -q --hard "$OLD"; echo "$NEW" > "$BAD"
    notify "🔴 обновление ${NEW:0:7} откатено: pip install упал"; exit 1; }
fi

TESTLOG="$STATE/autoupdate_tests.log"
if ! as_owner "$PY" -m pytest -q -x >"$TESTLOG" 2>&1; then
  g reset -q --hard "$OLD"
  echo "$NEW" > "$BAD"
  notify "🔴 обновление ${NEW:0:7} откатено: тесты не прошли
$(tail -n 15 "$TESTLOG")"
  exit 1
fi
rm -f "$BAD"

if echo "$CHANGED" | grep -qE '^deploy/.*\.(service|timer)$|^deploy/install_units.sh$'; then
  bash deploy/install_units.sh "$OWNER"
fi

# restart only what is running. Bots stop gracefully (cancel all orders, wait for settlements: up to ~15 min
# without quotes), so --no-block -- and not at all when only docs/reports/tests changed (frequent phone commits).
RESTARTED=()
RUNTIME="$(echo "$CHANGED" | grep -vE '\.md$|^reports/|^tests/' || true)"
if [ -n "$RUNTIME" ]; then
  for u in pmbot-5m pmbot-5m-b pmbot-5m-c pmbot-5m-d pmbot-5m-e pmbot-5m-f pmbot-5m-g pmbot-5m-g2 pmbot-5m-h pmbot-5m-live pmbot-lock pmbot-lock-wide pmbot-lock-15 pmbot-lock-maker pmbot-lock-maker2 pmbot-lock-tier pmbot-lock-tier-wide pmbot-lock-15-tier pmbot-lock-maker3 pmbot-lock-99 pmbot-lock-99-live pmbot-lock-tier-pb pmbot-lock-tier-late pmbot-lock-live pmbot-rewards pmbot-arb pmbot-hourly pmbot-tg; do
    # 3 s apart: ten Python processes starting at once (imports, tables, feeds) saturate the 2 vCPU for a minute
    if systemctl is-active -q "$u"; then systemctl restart --no-block "$u"; RESTARTED+=("$u"); sleep 3; fi
  done
fi
if echo "$CHANGED" | grep -qE '^(pmbot/recorder.py|scripts/record_live.py|deploy/pmbot-recorder.service)$' \
   && systemctl is-active -q pmbot-recorder; then
  systemctl restart --no-block pmbot-recorder; RESTARTED+=("pmbot-recorder")
fi

notify "✅ код обновлён ${OLD:0:7} → ${NEW:0:7} ($BRANCH), тесты ок
$LOG
перезапуск: ${RESTARTED[*]:-не нужен (менялись только документы/отчёты/тесты) или ничего не запущено}"
