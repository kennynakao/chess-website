#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
target="${1:-smart@chessboard.local}"
ssh "$target" 'mkdir -p ~/chess-relay/backend ~/chess-relay/scripts'
rsync -az --exclude=__pycache__ --exclude=server.py backend/ "$target:chess-relay/backend/"
rsync -az requirements.txt "$target:chess-relay/"
rsync -az scripts/setup-pi.sh "$target:chess-relay/scripts/"
ssh "$target" 'cd ~/chess-relay && bash scripts/setup-pi.sh'
