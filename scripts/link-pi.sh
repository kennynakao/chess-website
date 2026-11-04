#!/usr/bin/env bash
# Link a freshly installed Pi to the website. Run this from the Mac.
#
#   bash scripts/link-pi.sh /path/to/relay.env [smart@chessboard.local]
#
# relay.env holds CHESS_RELAY_TOKEN, which must match the value in Vercel, and
# CHESS_STATE_PATH. It is only copied if the Pi does not already have one.
# Safe to run again: each step skips or repeats harmlessly.
set -euo pipefail
cd "$(dirname "$0")/.."
relay_env="${1:?Usage: bash scripts/link-pi.sh /path/to/relay.env [smart@chessboard.local]}"
target="${2:-smart@chessboard.local}"

echo "== 1. Installing Stockfish and Python tools on the Pi =="
ssh -t "$target" 'sudo apt-get update && sudo apt-get install -y stockfish python3-venv rsync'

echo "== 2. Giving the Pi its relay settings =="
if ssh "$target" 'test -s ~/.config/duen-chess/relay.env'; then
  echo "The Pi already has relay.env, so it was left alone."
else
  [ -s "$relay_env" ] || { echo "No settings file at $relay_env"; exit 1; }
  ssh "$target" 'umask 077; mkdir -p ~/.config/duen-chess; cat > ~/.config/duen-chess/relay.env' < "$relay_env"
  echo "Copied."
fi

echo "== 3. Installing the game server =="
bash scripts/deploy-pi.sh "$target"

echo "== 4. Keeping it running after logout and reboot =="
ssh -t "$target" 'sudo loginctl enable-linger "$USER"'

echo "== 5. Connecting the Pi to the internet through Tailscale =="
echo "If a login link appears, open it and sign in. To keep the Pi's old web address,"
echo "first remove the old 'chessboard' device at https://login.tailscale.com/admin/machines"
ssh -t "$target" 'command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh; sudo tailscale up; sudo tailscale funnel --bg --yes 8765; tailscale funnel status'

echo
echo "== 6. Checking everything =="
bash scripts/pi-preflight.sh "$target" || true
echo
echo "PI_API_URL in Vercel must be the https address printed above, with nothing after .ts.net."
