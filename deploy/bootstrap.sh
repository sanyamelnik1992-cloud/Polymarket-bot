#!/usr/bin/env bash
# Fresh server (Ubuntu 22.04/24.04, run as root):
#   curl -fsSL https://raw.githubusercontent.com/sanyamelnik1992-cloud/Polymarket-bot/main/deploy/bootstrap.sh | bash -s -- main
# Creates user `pmbot`, clones the repo into /home/pmbot/Polymarket-bot on the given branch, makes a GitHub deploy key
# (printed at the end: add it in GitHub -> Settings -> Deploy keys with "Allow write access"), runs deploy/setup.sh.
# Fetch goes over HTTPS (works for a public repo without any key), push over SSH with the deploy key.
set -euo pipefail
BRANCH="${1:-main}"
REPO_HTTPS="https://github.com/sanyamelnik1992-cloud/Polymarket-bot.git"
REPO_SSH="git@github.com:sanyamelnik1992-cloud/Polymarket-bot.git"
U=pmbot
H=/home/$U
DIR=$H/Polymarket-bot

apt-get update -y && apt-get install -y git sudo
id "$U" >/dev/null 2>&1 || useradd -m -s /bin/bash "$U"

if [ ! -f "$H/.ssh/id_ed25519" ]; then
  sudo -u "$U" mkdir -p "$H/.ssh" && chmod 700 "$H/.ssh"
  sudo -u "$U" ssh-keygen -q -t ed25519 -N "" -C "pmbot-server-deploy" -f "$H/.ssh/id_ed25519"
  sudo -u "$U" bash -c "ssh-keyscan -t ed25519 github.com >> $H/.ssh/known_hosts 2>/dev/null"
fi

if [ ! -d "$DIR/.git" ]; then
  sudo -u "$U" git clone -q -b "$BRANCH" "$REPO_HTTPS" "$DIR"
fi
sudo -u "$U" git -C "$DIR" remote set-url --push origin "$REPO_SSH"
sudo -u "$U" git -C "$DIR" config user.name "pmbot server"
sudo -u "$U" git -C "$DIR" config user.email "pmbot@server"
mkdir -p /etc/pmbot
[ -f /etc/pmbot/pmbot.env ] || install -m 600 "$DIR/deploy/pmbot.env.example" /etc/pmbot/pmbot.env
sed -i "s/^PMBOT_BRANCH=.*/PMBOT_BRANCH=$BRANCH/" /etc/pmbot/pmbot.env
grep -q '^PMBOT_BRANCH=' /etc/pmbot/pmbot.env || echo "PMBOT_BRANCH=$BRANCH" >> /etc/pmbot/pmbot.env

SUDO_USER=$U bash "$DIR/deploy/setup.sh"

echo
echo "=== GitHub deploy key (Settings -> Deploy keys -> Add, tick 'Allow write access') ==="
cat "$H/.ssh/id_ed25519.pub"
