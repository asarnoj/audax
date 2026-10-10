#!/usr/bin/env bash
# Creates workspace/memory from templates (never overwrites) and gives it its own local git repo.
set -euo pipefail
cd "$(dirname "$0")/.."

mem=workspace/memory
mkdir -p "$mem"
# Copy templates that don't exist yet (portable across GNU and BSD cp).
(cd templates/memory && find . -type f) | while read -r f; do
  [ -e "$mem/$f" ] || { mkdir -p "$mem/$(dirname "$f")"; cp "templates/memory/$f" "$mem/$f"; }
done

if [ ! -d "$mem/.git" ]; then
  git -C "$mem" init -q -b main
  git -C "$mem" add -A
  git -C "$mem" -c user.name=agent -c user.email=agent@localhost commit -q -m "Init memory from templates"
  echo "Minnet skapat i $mem (lokalt git-repo, pushas aldrig)."
else
  echo "Minnet finns redan i $mem, nya mallfiler (om några) har lagts till."
fi
