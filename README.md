# DUEN Chess — remote Raspberry Pi play

Website: https://chess-website-gules.vercel.app

## How a move travels

Browser → Vercel API → authenticated HTTPS tunnel → Raspberry Pi → gantry → camera →
Stockfish → gantry → camera → browser. The Pi validates each move and saves the game. The Mac
is only a development/deployment tool; it is not in the live connection path.

The Pi must remain powered on and online. No router port forwarding is needed.
No gantry or camera is connected yet, so nothing physically moves. Website games
already run through both stages. See "How a website move runs".

## Repository layout

- `index.html`, `landing.js`, `landing.css`: the animated landing page.
- `how-it-works.js`, `how-it-works.css`: the 3D "How a move travels" walkthrough on that page.
- `play.html`, `app.js`, `chess.js`, `style.css`: the chess interface.
- `assets/models/`: the chess piece models (STL) shown in the landing scene, and the
  credited walkthrough models (laptop, Pi, relay, chess set).
- `assets/vendor/`: the locally served Three.js runtime and its license.
- `api/[action].js`: Vercel login/session handling and server-only Pi proxy.
- `backend/remote_server.py`: loopback-only authenticated Pi API and SQLite state.
- `backend/chess_game.py`: shared rules and Stockfish integration.
- `backend/board_motion.py`: turns moves into physical steps and runs the gantry program.
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
requests return the original result; stale commands are rejected. Each move commits
atomically with the job it owes the board. SQLite preserves history and retry results
across service restarts, and a restart resumes a game from the stage it had reached. After interruption, the browser reloads the Pi's
confirmed UCI history. It never silently substitutes a local casual bot.

## Three ways to play

The Pi has three separate game functions, and none of them changes another:

* **In person on the touchscreen**, player against the bot: `pi_game.py` and `pi_touch.py`
  from the `pi-only` branch of Self-Moving-Chess-Board-Code. This service never touches
  them; it only reads their camera calibration.
* **The digital game on the website** ("On screen", the default): Stockfish answers inside
  the same request and nothing physical moves.
* **The board game on the website** ("On the board"): every move runs through the physical
  board in this order, and each stage starts only when the one before it has finished:

  1. The player moves on the website, and the move is saved on the Pi.
  2. The gantry moves that piece.
  3. The camera checks the board.
  4. Stockfish chooses its move, which is saved on the Pi.
  5. The gantry moves that piece.
  6. The camera checks the board.
  7. The turn goes back to the player.

  `backend/board_pipeline.py` is that pipeline. If a stage fails, the game stops there and
  the website says so. Set the pieces to match the screen and press **Pieces match the
  screen**; the game carries on from that point. Every stage is logged with the prefix
  `PIPELINE`, which `scripts/pi-logs.sh` shows live. The Pi remembers where the physical
  pieces were last left, so a board game started after digital games begins by putting them
  back.

The choice is made per game on the play screen and sent as `"board": true` or `false` with
`/api/new`; the Pi reports it back as `board` in every state.

### The gantry slot

The gantry code is not written yet. The plan is Pi-to-Arduino: the Pi sends each step over
USB serial and an Arduino drives the motors. `backend/gantry.py` is the placeholder for that
code, with the job format and what the finished driver has to do. `CHESS_GANTRY=arduino`
selects it, which today stops every board game at its first move with "not written yet",
so leave it unset until the code exists. Until then board games log each job and carry on,
or a motor program in any language can take the slot through `CHESS_MOTION_COMMAND` in
`relay.env`:

```sh
CHESS_MOTION_COMMAND="/home/smart/gantry/run_job --square-mm 57"
```

It runs once per chess move with the job as JSON on standard input, and returns once the
pieces have stopped: exit status 0 means done, anything else stops the game. The header of
`backend/board_motion.py` documents the job format, including captures, castling, en
passant, promotion, takebacks, and the reset for a new game.

Carried pieces pass others along the lanes between squares. The largest piece base in
`assets/models/` is 27 mm wide, so two can only pass side by side if squares are at least
54 mm. A promotion uses a captured piece of that type if there is one; otherwise the pawn
stays on the board and stands in for it.

### The camera slot

The service uses the vision code from a checkout of the `pi-only` branch at
`~/chess-board-pi-only` or `~/Self-Moving-Chess-Board-Code`, under that checkout's own
`.venv`. The camera switches on once the checkout has a calibration, and it checks the
board only after a gantry has really moved something. `backend/board_camera.py` lists the
settings. Restart `duen-relay` after calibrating.

`CHESS_BOARD_MODE=manual` puts a person in the gantry's place: the website says what to
move, and **Done moving** takes the next step.

`scripts/link-pi.sh` sets up a fresh Pi and `scripts/pi-preflight.sh` reports what is
missing.

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

The landing scene uses a DUEN purple backdrop, a floating gold pawn, and a
scroll-driven opening move. It pauses rendering when settled or out of view,
respects reduced motion, and caps its drawing buffer on large displays.
