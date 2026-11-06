#!/usr/bin/env bash
# Hourly: write scripts/server_report.py output into a separate worktree on branch `server-reports` and push it.
# Code branches are never touched. Runs as the repo owner (pmbot-reports.service), needs the deploy key with write access.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WT="$REPO/data/server-reports"
BR=server-reports
cd "$REPO"

if [ ! -d "$WT/.git" ] && [ ! -f "$WT/.git" ]; then
  git worktree prune
  if git ls-remote --exit-code --heads origin "$BR" >/dev/null 2>&1; then
    git fetch -q origin "$BR"
    git worktree add -q -B "$BR" "$WT" "origin/$BR"
  else
    git worktree add -q --orphan -b "$BR" "$WT"
  fi
fi

"$REPO/.venv/bin/python" scripts/server_report.py "$WT"
cd "$WT"
git add -A
if git diff --cached --quiet; then exit 0; fi
git -c user.name="pmbot server" -c user.email="pmbot@server" commit -q -m "server report $(date -u '+%Y-%m-%d %H:%M') UTC"
git push -q origin "$BR"
