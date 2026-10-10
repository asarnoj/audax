#!/usr/bin/env bash
# Sets up the agent on a Raspberry Pi (64-bit Raspberry Pi OS Bookworm). Safe to re-run.
# Usage, from the cloned repo on the Pi:  ./deploy/setup-pi.sh
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

step "Kontrollerar arkitektur"
arch="$(uname -m)"
if [ "$arch" != "aarch64" ]; then
  echo "Claude Code kräver 64-bitars OS (aarch64), du kör $arch. Installera 64-bitars Raspberry Pi OS först."
  exit 1
fi
free -h | awk '/Mem:/ {print "RAM: " $2}'

step "Systempaket"
sudo apt-get update -qq
sudo apt-get install -y -qq git curl ca-certificates sqlite3

step "uv (Python-pakethanterare)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv --version

step "Claude Code"
if ! command -v claude >/dev/null; then
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
fi
claude --version

step "Python-beroenden"
uv sync --no-dev

step "Konfiguration"
[ -f .env ] || { cp .env.example .env; echo "Skapade .env, fyll i TELEGRAM_TOKEN."; }
uv run python -c "from agent.config import ensure_instance; print('instans:', ensure_instance() or 'finns redan')"
./deploy/init-memory.sh

step "systemd-tjänst"
sed -e "s|__USER__|$USER|g" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
  deploy/agent.service | sudo tee /etc/systemd/system/agent.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable agent.service >/dev/null
echo "Tjänsten är installerad men startas inte automatiskt här."

cat <<EOF

Klart. Kvar att göra manuellt:
  1. Logga in Claude Code (en gång):   claude   → följ inloggningen, avsluta sedan med /exit
  2. Fyll i TELEGRAM_TOKEN i .env och ditt namn (owner) i instance/config/settings.yaml
  3. Kör smoketest:                    uv run python -m agent.smoketest
  4. Starta:                           sudo systemctl start agent && journalctl -u agent -f
  5. Skriv /start till boten, lägg in chat-id:t i .env, och: sudo systemctl restart agent
EOF
