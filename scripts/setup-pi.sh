#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
config="$HOME/.config/duen-chess/relay.env"
if [ ! -s "$config" ]; then
  echo "Missing private configuration: $config"; exit 1
fi
mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/state/duen-chess"
chmod 700 "$HOME/.config/duen-chess" "$HOME/.local/state/duen-chess"
chmod 600 "$config"
cat > "$HOME/.config/systemd/user/duen-relay.service" <<EOF
[Unit]
Description=DUEN authenticated remote chess API
After=network-online.target
[Service]
WorkingDirectory=$PWD
EnvironmentFile=$config
ExecStart=$PWD/.venv/bin/python -u $PWD/backend/remote_server.py --port 8765
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
UMask=0077
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now duen-relay.service
systemctl --user restart duen-relay.service
systemctl --user --no-pager status duen-relay.service
