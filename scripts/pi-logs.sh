#!/usr/bin/env bash
set -euo pipefail
exec ssh -t "${1:-smart@chessboard.local}" 'journalctl _SYSTEMD_USER_UNIT=duen-relay.service -n 40 -f -o cat'
