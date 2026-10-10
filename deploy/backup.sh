#!/usr/bin/env bash
# Backs up what exists only on the Pi to this Mac: the memory repo and the instance repo (full git
# history each), and the database (nightly snapshots + a fresh one). .env is NOT copied (secrets).
# If the instance backup has a "github" remote (a private GitHub repo), it is pushed there too.
# Runs daily via launchd (deploy/audax-backup.plist); safe to run by hand.
set -euo pipefail
# Pi host (user@host) from .deploy.env (gitignored) or the environment.
[ -f "$(dirname "$0")/../.deploy.env" ] && . "$(dirname "$0")/../.deploy.env"
: "${PI_HOST:?Sätt PI_HOST=user@host i .deploy.env}"
DEST="${BACKUP_DIR:-$HOME/Backups/audax}"
REMOTE=${PI_DIR:-Projekt/audax}
mkdir -p "$DEST/db"

# Memory and instance: git mirrors, so every change the agent ever made is restorable.
mirror() {  # mirror <remote path> <local dir>
  if [ -d "$2/.git" ]; then git -C "$2" pull -q --ff-only; else git clone -q "$PI_HOST:$1" "$2"; fi
}
mirror "$REMOTE/workspace/memory" "$DEST/memory"
mirror "$REMOTE/instance" "$DEST/instance"
if git -C "$DEST/instance" remote get-url github >/dev/null 2>&1; then
  git -C "$DEST/instance" push -q github main && echo "Instansen pushad till $(git -C "$DEST/instance" remote get-url github)"
fi

# Database: a consistent snapshot now, plus the Pi's nightly ones (7 days).
ssh -o BatchMode=yes "$PI_HOST" "cd $REMOTE && mkdir -p data/backups && sqlite3 data/agent.db \".backup data/backups/agent-latest.db\""
rsync -a "$PI_HOST:$REMOTE/data/backups/" "$DEST/db/"

date "+%Y-%m-%d %H:%M" > "$DEST/last-backup"
echo "Backup klar: $(du -sh "$DEST" | cut -f1) i $DEST"
