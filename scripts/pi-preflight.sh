#!/usr/bin/env bash
# Read-only readiness check for a real-website chess test. Run from the Mac:
#   bash scripts/pi-preflight.sh [smart@chessboard.local]
# One ssh connection; nothing is written, installed or restarted on the Pi, no sudo
# is used, and no secret value is printed. PREFLIGHT_LOCAL=1 runs the same checks
# in a local bash (against $HOME) instead of over ssh, for testing without a Pi.
# Exit: 0 READY / READY WITH WARNINGS, 2 NOT READY, 3 ssh could not connect.
set -euo pipefail
cd "$(dirname "$0")/.."
target="${1:-smart@chessboard.local}"

IFS= read -r -d '' body <<'REMOTE' || true
target="${1:-smart@chessboard.local}"
set -u
export PATH="$PATH:/usr/games:/usr/local/bin:/usr/sbin:/sbin"
fails=0; warns=0
pass() { printf 'PASS  %s\n' "$1"; }
warn() { warns=$((warns + 1)); printf 'WARN  %s\n' "$1"; [ -z "${2:-}" ] || printf '      next: %s\n' "$2"; }
fail() { fails=$((fails + 1)); printf 'FAIL  %s\n' "$1"; [ -z "${2:-}" ] || printf '      next: %s\n' "$2"; }
have() { command -v "$1" >/dev/null 2>&1; }

me=$(id -un 2>/dev/null || echo smart)
relay="$HOME/chess-relay"
envf="$HOME/.config/duen-chess/relay.env"
deploy="bash scripts/deploy-pi.sh $target"

# Value of NAME in relay.env (last assignment, quotes stripped). Never echo the
# result of this for secrets; callers only measure or test it.
envval() {
  [ -r "$envf" ] || return 0
  sed -nE "s/^[[:space:]]*(export[[:space:]]+)?$1=//p" "$envf" | tr -d '\r' | tail -n 1 \
    | sed -E -e 's/^"(.*)"$/\1/' -e "s/^'(.*)'\$/\1/" || true
}
envhas() { [ -r "$envf" ] && grep -Eq "^[[:space:]]*(export[[:space:]]+)?$1=" "$envf"; }

echo "== Game server =="
missing=""
for f in remote_server.py board_pipeline.py board_motion.py board_camera.py gantry.py vision_bridge.py; do
  [ -f "$relay/backend/$f" ] || missing="$missing $f"
done
if [ -d "$relay/backend" ] && [ -z "$missing" ]; then
  pass "~/chess-relay/backend has all the game files"
else
  fail "~/chess-relay/backend missing:${missing:- the directory}" "$deploy"
fi

if [ -x "$relay/.venv/bin/python" ] && "$relay/.venv/bin/python" -I -c 'import chess' </dev/null >/dev/null 2>&1; then
  pass "~/chess-relay/.venv can import python-chess"
else
  fail "~/chess-relay/.venv missing or python-chess not importable" "$deploy"
fi

if [ ! -f "$envf" ]; then
  fail "relay.env not found at ~/.config/duen-chess/relay.env" \
    "on the Pi: mkdir -p ~/.config/duen-chess && (umask 077; printf 'CHESS_RELAY_TOKEN=%s\\nCHESS_STATE_PATH=%s\\n' \"\$(openssl rand -hex 32)\" \"\$HOME/.local/state/duen-chess/game.sqlite3\" > ~/.config/duen-chess/relay.env)  (the same token goes into Vercel)"
else
  mode=$(stat -c %a "$envf" 2>/dev/null || stat -f %Lp "$envf" 2>/dev/null || echo unknown)
  if [ "$mode" = 600 ]; then pass "relay.env mode is 600"
  else fail "relay.env mode is $mode, expected 600" "chmod 600 ~/.config/duen-chess/relay.env"; fi
  for name in CHESS_RELAY_TOKEN CHESS_STATE_PATH; do
    if envhas "$name"; then pass "relay.env defines $name"
    else fail "relay.env does not define $name" "add a $name=... line to ~/.config/duen-chess/relay.env"; fi
  done
  tok=$(envval CHESS_RELAY_TOKEN)
  if [ -n "$tok" ]; then
    if [ "${#tok}" -ge 32 ]; then pass "CHESS_RELAY_TOKEN is at least 32 characters"
    else fail "CHESS_RELAY_TOKEN is shorter than 32 characters" "set a longer token: openssl rand -hex 32 (and update Vercel to match)"; fi
  fi
  tok=""
fi

# systemctl --user needs the user's runtime dir, which a bare ssh session may lack.
if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
fi
if have systemctl && [ "$(systemctl --user is-active duen-relay.service 2>/dev/null </dev/null || true)" = active ]; then
  pass "duen-relay user service is active"
else
  fail "duen-relay user service is not active" "$deploy   (then: bash scripts/pi-logs.sh $target)"
fi

linger=""
if have loginctl; then
  linger=$(loginctl show-user "$me" -p Linger 2>/dev/null </dev/null | sed -n 's/^Linger=//p' || true)
fi
if [ "$linger" = yes ]; then pass "linger is enabled for $me"
else fail "linger is not enabled for $me (service stops at logout)" "sudo loginctl enable-linger $me"; fi

code=000
if have curl; then
  code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:8765/api/status 2>/dev/null </dev/null || true)
fi
case "$code" in
  401) pass "GET /api/status without a token returns 401 (server up, auth enforced)" ;;
  000|"") fail "nothing answered on http://127.0.0.1:8765/api/status" "check the service: bash scripts/pi-logs.sh $target" ;;
  *) fail "GET /api/status without a token returned HTTP $code, expected 401" "check CHESS_RELAY_TOKEN in relay.env, then $deploy" ;;
esac

echo "== Engine and network =="
if have stockfish; then pass "stockfish found"
else fail "stockfish not found on PATH or /usr/games" "sudo apt install stockfish"; fi
if have rsync; then pass "rsync installed"
else fail "rsync not installed" "sudo apt install rsync"; fi

if ! have tailscale; then
  fail "tailscale not installed" "curl -fsSL https://tailscale.com/install.sh | sh   then: sudo tailscale up"
else
  if tailscale status </dev/null >/dev/null 2>&1; then pass "tailscale is logged in"
  else fail "tailscale is not logged in or not running" "sudo tailscale up"; fi
  fs=$(tailscale funnel status 2>&1 </dev/null || true)
  if printf '%s' "$fs" | grep -Eqi 'access denied|permission denied|sudo|must be root|operator'; then
    warn "cannot read funnel status without sudo" "on the Pi: sudo tailscale funnel status   (expect port 8765; otherwise: sudo tailscale funnel --bg --yes 8765)"
  elif printf '%s' "$fs" | grep -Eq '(^|[^0-9])8765([^0-9]|$)'; then
    url=$(printf '%s' "$fs" | grep -Eo 'https://[^[:space:]]+' | head -n 1 | sed 's/:$//' || true)
    pass "tailscale funnel serves port 8765"
    [ -z "$url" ] || echo "      public URL: $url   (must match PI_API_URL in Vercel)"
  else
    fail "tailscale funnel is not publishing port 8765" "sudo tailscale funnel --bg --yes 8765"
  fi
fi

echo "== Board code (pi-only branch of Self-Moving-Chess-Board-Code) =="
vdir=$(envval CHESS_VISION_DIR)
if [ -z "$vdir" ]; then
  vdir="$HOME/chess-board-pi-only"
  [ -f "$vdir/board_vision.py" ] || vdir="$HOME/Self-Moving-Chess-Board-Code"
fi
case "$vdir" in "~") vdir="$HOME" ;; "~/"*) vdir="$HOME/${vdir#\~/}" ;; esac
vmiss=""
for f in board_vision.py pi_camera.py pi_game.py; do [ -f "$vdir/$f" ] || vmiss="$vmiss $f"; done
clone="git clone -b pi-only https://github.com/hiyab-tesfay/Self-Moving-Chess-Board-Code.git ~/Self-Moving-Chess-Board-Code  (then create its .venv and install opencv + python-chess)"
if [ -d "$vdir" ] && [ -z "$vmiss" ]; then
  pass "board checkout at $vdir has the vision files"
  if [ -e "$vdir/.git" ] && have git; then
    br=$(git --no-optional-locks -C "$vdir" rev-parse --abbrev-ref HEAD 2>/dev/null </dev/null || true)
    if [ -n "$br" ] && [ "$br" != pi-only ]; then warn "board checkout is on branch '$br', expected pi-only" "git -C $vdir checkout pi-only"; fi
  fi
else
  fail "board checkout at $vdir missing:${vmiss:- the directory}" "$clone"
fi

vpy="$vdir/.venv/bin/python"
if [ -x "$vpy" ] && (cd / && "$vpy" -I -c 'import cv2, chess') </dev/null >/dev/null 2>&1; then
  pass "board .venv can import cv2 and chess"
else
  warn "board .venv/bin/python missing or cannot import cv2, chess (camera checks stay off; the game still runs by hand)" \
    "cd $vdir && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
fi

cdir=$(envval CHESS_CALIBRATION_DIR)
[ -n "$cdir" ] || cdir="$vdir/calibration"
case "$cdir" in "~/"*) cdir="$HOME/${cdir#\~/}" ;; esac
if [ -f "$cdir/calibration.json" ] && [ -f "$cdir/empty_warp.png" ]; then
  pass "calibration.json and empty_warp.png exist"
else
  warn "calibration incomplete in $cdir (camera checks stay off; the game still runs by hand)" "cd $vdir && .venv/bin/python pi_game.py setup   (run at the board, then restart duen-relay)"
fi

# /dev/video* exists on a Pi with no camera attached, so ask the camera stack itself.
camera_seen=""
for tool in rpicam-hello libcamera-hello; do
  if have "$tool" && "$tool" --list-cameras 2>/dev/null </dev/null | grep -q 'Available cameras'; then camera_seen="$tool"; break; fi
done
if [ -n "$camera_seen" ]; then
  pass "camera detected by $camera_seen"
elif compgen -G '/dev/v4l/by-id/*' >/dev/null 2>&1; then
  pass "USB camera detected"
else
  warn "no camera detected (camera checks stay off; the game still runs by hand)" "connect the camera and check its ribbon cable, then: rpicam-hello --list-cameras"
fi

echo "== Motion mode =="
if envhas CHESS_MOTION_COMMAND && [ -n "$(envval CHESS_MOTION_COMMAND)" ]; then
  pass "mode: command (CHESS_MOTION_COMMAND is set; a motor program moves the pieces)"
elif [ "$(envval CHESS_GANTRY | tr '[:upper:]' '[:lower:]')" = arduino ]; then
  warn "mode: arduino (the Pi-to-Arduino gantry code in backend/gantry.py is not written yet: board games stop at their first move)" \
    "leave CHESS_GANTRY unset in relay.env until that code exists; digital games are not affected"
else
  bm=$(envval CHESS_BOARD_MODE)
  if [ "$bm" = manual ]; then
    pass "mode: manual (a person moves pieces and confirms on the website)"
  else
    warn "gantry not connected (website games run and log each job, nothing physically moves)" "when the gantry program exists, set CHESS_MOTION_COMMAND in relay.env and restart duen-relay"
  fi
fi

echo
if [ "$fails" -gt 0 ]; then
  echo "NOT READY: $fails problems"; exit 2
elif [ "$warns" -gt 0 ]; then
  echo "READY WITH WARNINGS"; exit 0
else
  echo "READY"; exit 0
fi
REMOTE

rc=0
if [ "${PREFLIGHT_LOCAL:-}" = 1 ]; then
  bash -c "$body" preflight "$target" </dev/null || rc=$?
else
  # The remote shell reads the body from stdin, then runs it with bash -c so the
  # checks themselves have a clean stdin.
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$target" \
    'bash -c "$(cat)" preflight '"$(printf '%q' "$target")" <<<"$body" || rc=$?
fi

case "$rc" in
  0|2) exit "$rc" ;;
  255) echo "Cannot connect to $target without a password. Run: ssh-copy-id $target" >&2; exit 3 ;;
  *) echo "pi-preflight: checks ended unexpectedly (exit $rc)" >&2; exit 1 ;;
esac
