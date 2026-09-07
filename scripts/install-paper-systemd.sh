#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOT="$PROJECT_DIR/.venv/bin/crypto-bot"

if [[ ! -x "$BOT" ]]; then
  echo "Missing $BOT"
  echo "Create the venv first: python3 -m venv .venv && source .venv/bin/activate && pip install -e ."
  exit 1
fi

SERVICE=/etc/systemd/system/crypto-bot-paper.service
TIMER=/etc/systemd/system/crypto-bot-paper.timer
USER_NAME="${SUDO_USER:-$USER}"
PROJECT_ESCAPED=$(printf '%q' "$PROJECT_DIR")
BOT_ESCAPED=$(printf '%q' "$BOT")

sudo tee "$SERVICE" >/dev/null <<EOF
[Unit]
Description=Crypto Bot persistent forward paper tick
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$PROJECT_DIR
ExecStart=/usr/bin/env bash -lc 'cd $PROJECT_ESCAPED && $BOT_ESCAPED paper tick'

[Install]
WantedBy=multi-user.target
EOF

sudo tee "$TIMER" >/dev/null <<'EOF'
[Unit]
Description=Run Crypto Bot paper tick daily after UTC close

[Timer]
OnCalendar=*-*-* 00:10:00 UTC
Persistent=true
Unit=crypto-bot-paper.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now crypto-bot-paper.timer

echo "Installed. Next run:"
sudo systemctl list-timers crypto-bot-paper.timer --no-pager
echo "Logs: journalctl -u crypto-bot-paper.service"
