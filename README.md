# DUEN Chess — remote Raspberry Pi play

Website: https://chess-website-gules.vercel.app

## How a move travels

Browser → Vercel API → authenticated HTTPS tunnel → Raspberry Pi → Stockfish → browser.
The Pi validates both moves and saves the game before returning a reply. The Mac
is only a development/deployment tool; it is not in the live connection path.

The Pi must remain powered on and online. No router port forwarding is needed.
The Pi works out the physical steps for every move and queues them, but no motor
program is connected yet, so nothing physically moves. See "Board movement".

## Repository layout

- `index.html`, `landing.js`, `landing.css`: the animated landing page.
- `concept.html`, `concept.css`, `how-it-works.js`: the landing page in the DUEN colors with
  the 3D "How a move travels" walkthrough. Not linked from the site.
- `play.html`, `app.js`, `chess.js`, `style.css`: the chess interface.
- `assets/models/`: the chess piece models (STL) shown in the landing scene, and the
  credited walkthrough models (laptop, Pi, relay, chess set) used by `concept.html`.
- `assets/vendor/`: the locally served Three.js runtime and its license.
- `api/[action].js`: Vercel login/session handling and server-only Pi proxy.
- `backend/remote_server.py`: loopback-only authenticated Pi API and SQLite state.
- `backend/chess_game.py`: shared rules and Stockfish integration.
- `backend/board_motion.py`: turns moves into physical steps and runs the motor program.
- `scripts/`: static build, Pi deployment, and live logs.
- `test/`: rules, premoves, remote client, backend, and proxy checks.

## Credentials

Vercel production environment variables:

- `PI_API_URL`: stable HTTPS URL of the Pi's tunnel (origin only).
- `CHESS_RELAY_TOKEN`: random 32+ character server-to-server token.
- `CHESS_SESSION_SECRET`: random key used to sign browser sessions.
- `CHESS_ACCESS_CODE`: private board access code entered on the website.

The Pi stores only `CHESS_RELAY_TOKEN`, `CHESS_STATE_PATH` and the optional
`CHESS_MOTION_COMMAND` in `~/.config/duen-chess/relay.env` (mode 600). Never commit
these values.
The relay token is never returned to a browser. The player cookie is Secure,
HttpOnly, and SameSite=Strict and expires after 24 hours.

One browser session controls the board at a time. Other sessions can view it.
The control lease lasts one hour, refreshed by successful commands. Remote play
is untimed; clocks must be implemented on the Pi before timed remote play.

## Pi setup

Install Python venv support, rsync and Stockfish on the Pi. Set the two variables
in `~/.config/duen-chess/relay.env`, with `CHESS_STATE_PATH` outside the source
checkout, such as `/home/smart/.local/state/duen-chess/game.sqlite3`.

From your Mac:

```sh
bash scripts/deploy-pi.sh smart@chessboard.local
```

Once on the Pi, enable the service across logout/reboot:

```sh
sudo loginctl enable-linger smart
```

With Tailscale installed and signed into an account that permits Funnel:

```sh
sudo tailscale funnel --bg --yes 8765
```

Funnel supplies a stable HTTPS address. Only the API on `127.0.0.1:8765` is
published; it requires the relay token and does not serve repository files.
The Tailscale service and `duen-relay` user service run independently of SSH.
Review Tailscale device key expiry if the board will be left unattended long term.

## Watch moves on the Pi

```sh
bash scripts/pi-logs.sh
```

Logs show the human move, Stockfish thinking, the reply, and revision/request
identifiers. Ctrl-C closes the log viewer without stopping the server.

## Reliability

Every mutation carries a request ID and expected game ID/revision. Duplicate
requests return the original result; stale commands are rejected. Human and
engine moves commit atomically. SQLite preserves history and retry results
across service restarts. After interruption, the browser reloads the Pi's
confirmed UCI history. It never silently substitutes a local casual bot.

## Board movement

Every committed move becomes a job in the same SQLite transaction as the game, so a
restart cannot lose one. Jobs run one at a time in the background, and the game does
not wait for them. Captures, en passant, castling, promotion, takebacks, and the
reset for a new game are all planned in `backend/board_motion.py`, whose header
documents the job format.

Connect the motors by pointing `CHESS_MOTION_COMMAND` at a program, in any language:

```sh
CHESS_MOTION_COMMAND="/home/smart/motors/run_job --square-mm 57"
```

It runs once per job with the job as JSON on standard input, and returns once the
pieces have stopped: exit status 0 means done, anything else stops the queue. It is
the only part that needs to know about pins, drivers, and square size. Without it,
the Pi only logs the jobs, and the website says the motors are not connected.

After a failure, or a restart in the middle of a job, the website blocks new moves.
Set the pieces to match the screen, with captured pieces in their parking slots, and
press **Pieces match the screen** to clear the queue.

Carried pieces pass others along the lanes between squares. The largest piece base
in `assets/models/` is 27 mm wide, so two can only pass side by side if squares are
at least 54 mm. A promotion uses a captured piece of that type if there is one;
otherwise the pawn stays on the board and stands in for it.

## Validation

```sh
node test/perft.js
node test/premove.js
node test/remote-client.js
node --test test/proxy.test.js
python -m unittest discover -s test -p 'test_*.py'
```

Vercel builds `public/` with `scripts/build-web.sh` and deploys only the static
landing page, chess interface, and the `api/` function. The landing page links
to `/play.html`; the existing Pi connection remains on that game page.

The landing scene uses a white backdrop, a floating silver pawn, and a
scroll-driven opening move. It pauses rendering when settled or out of view,
respects reduced motion, and caps its drawing buffer on large displays.
