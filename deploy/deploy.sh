#!/usr/bin/env bash
# Deploys from the Mac to the Pi over Tailscale: pulls build-mode changes, runs tests, pushes, syncs, restarts.
# Usage: ./deploy/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."
# Pi host (user@host) from .deploy.env (gitignored) or the environment.
[ -f "$(dirname "$0")/../.deploy.env" ] && . "$(dirname "$0")/../.deploy.env"
: "${PI_HOST:?Sätt PI_HOST=user@host i .deploy.env}"

if [ -n "$(git status --porcelain)" ]; then
  echo "Det finns ocommittade ändringar. Committa först."; exit 1
fi
# Build mode commits on the Pi; bring those changes here first.
git pull --no-rebase --no-edit pi main
uv run pytest -q
git push pi main
ssh "$PI_HOST" "cd ${PI_DIR:-Projekt/audax} && ./deploy/init-memory.sh >/dev/null && ~/.local/bin/uv sync --no-dev -q && sudo systemctl restart agent $(systemctl list-units --plain --no-legend --all 'agent@*' | awk '{print $1}') && sleep 8 && systemctl is-active agent && journalctl -u agent --no-pager -n 3 -o cat"
# The Pi pushes to GitHub (it has the GitHub key); this machine doesn't need one.
ssh "$PI_HOST" "cd ${PI_DIR:-Projekt/audax} && git remote get-url github >/dev/null 2>&1 && git push -q github main && echo 'GitHub: pushad' || echo '(inget github-remote på Pi:n)'"
