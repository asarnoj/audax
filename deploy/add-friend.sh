#!/usr/bin/env bash
# Sets up Audax for a friend on this machine (the Pi), on the host's Claude subscription, with a budget cap.
# Run on the Pi from the engine's folder:   ./deploy/add-friend.sh <name> "<their first name>" [weekly cap in $, default 7]
# You need a Telegram bot for them first: @BotFather -> /newbot. The script asks for its token (not echoed).
# It prints an invite link; the friend opens it and taps Start, and Audax begins by getting to know them.
set -euo pipefail
cd "$(dirname "$0")/.."
name="${1:?namn, t.ex. anna}"; owner="${2:?vännens förnamn}"; cap="${3:-7}"
[[ "$name" =~ ^[a-z0-9-]+$ ]] || { echo "Namnet får bara innehålla a-z, 0-9 och -"; exit 1; }
home="$HOME/audax-friends/$name"
[ -e "$home" ] && { echo "$home finns redan."; exit 1; }
read -rsp "Bot-token från @BotFather: " token; echo
[ -n "$token" ] || { echo "Ingen token."; exit 1; }
username=$(curl -fsS "https://api.telegram.org/bot${token}/getMe" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["username"])') \
  || { echo "Telegram känner inte igen den token."; exit 1; }
invite=$(python3 -c 'import secrets; print(secrets.token_hex(8))')
host_name=$(~/.local/bin/uv run --no-dev python -c 'from agent.config import load_settings; print(load_settings().owner)')
host_pronoun=$(~/.local/bin/uv run --no-dev python -c 'from agent.config import load_settings; print(load_settings().pronoun)')

mkdir -p "$home"
umask 077
printf 'TELEGRAM_TOKEN=%s\nAUDAX_INVITE=%s\n' "$token" "$invite" > "$home/.env"
AUDAX_HOME="$home" ~/.local/bin/uv run --no-dev python - "$owner" "$host_name" "$cap" "$host_pronoun" <<'PY'
import sys
import yaml
from agent.config import ensure_instance, instance_config
owner, host, cap, pronoun = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
ensure_instance()
path = instance_config("settings.yaml")
s = yaml.safe_load(path.read_text()) or {}
s.update(owner=owner, host=host, host_pronoun=pronoun, builder=False, site={"enabled": False})
s.setdefault("models", {})["reflect"] = "sonnet"   # a friend's test: the lighter reflection
s["budget"] = {"cap_usd_week": cap, "night_reserve": 0.4}
path.write_text(yaml.safe_dump(s, allow_unicode=True, sort_keys=False))
PY

repo=$(pwd)
sed -e "s|__USER__|$USER|g" -e "s|__REPO__|$repo|g" -e "s|__HOME__|$HOME|g" deploy/agent@.service \
  | sudo tee /etc/systemd/system/agent@.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now "agent@$name" >/dev/null
sleep 6
systemctl is-active "agent@$name" >/dev/null && echo "Igång: agent@$name (tak ${cap} \$/vecka)." || { echo "Startade inte:"; journalctl -u "agent@$name" -n 20 --no-pager -o cat; exit 1; }
echo
echo "Skicka den här länken till $owner. Hen öppnar den och trycker Start:"
echo "  https://t.me/${username}?start=${invite}"
