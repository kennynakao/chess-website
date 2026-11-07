# DUEN Chess — remote Raspberry Pi play

Website: https://chess-website-gules.vercel.app

## How a move travels

Browser → Vercel API → authenticated HTTPS tunnel → Raspberry Pi. In the digital game the Pi
asks Stockfish for the reply; in a game on the board the gantry carries the move, the camera
checks it, and the reply is the move the person at the board makes by hand. The Pi validates
each move and saves the game. The Mac is only a development/deployment tool; it is not in the
live connection path.

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

The Pi stores only `CHESS_RELAY_TOKEN`, `CHESS_STATE_PATH` and the optional gantry settings
(`CHESS_MOTION_COMMAND`, or `CHESS_GANTRY=arduino` with `CHESS_GANTRY_PORT`; see "The gantry
slot") in `~/.config/duen-chess/relay.env` (mode 600). Never commit these values.
The relay token is never returned to a browser. The player cookie is Secure,
HttpOnly, and SameSite=Strict and expires after 24 hours.

One browser session controls the board at a time. Other sessions can view it.
The control lease lasts one hour, refreshed by successful commands, and ends with the game
(see "How a website game ends"). The time control picked on the play screen applies to Pi games
too. The clocks run in the browser, not on the Pi (Stockfish thinks for a fixed time per level
and knows nothing about them): they start with White's first move, wait while the board carries,
checks or waits for pieces, are kept in the browser's storage so a reloaded tab carries on where
it was, and when one reaches zero the website reports it to the Pi.

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
* **The board game on the website** ("On the board"): two people and no Stockfish. The person
  at the physical board plays White and the website player plays Black. Each stage starts only
  when the one before it has finished:

  1. The person at the board moves a White piece by hand and taps Done on the board's screen,
     which sends `POST /api/board-move`. The camera reads the move off the board and it is
     saved on the Pi; nothing is sent to the gantry, because the piece is already there.
  2. The website player moves Black, and the move is saved on the Pi.
  3. The gantry moves that piece.
  4. The camera checks the board.
  5. The turn goes back to the person at the board.

  Takebacks are off (`/api/undo` answers 409 `no_takebacks`), and every state says who plays
  what: `playerColor` is the website player's colour (`"b"` on the board, `"w"` in the digital
  game) and `boardColor` the colour of the person at the board (`"w"`, or null).
  `backend/board_pipeline.py` is the pipeline. If a stage fails, the game stops there and
  the website says so. Set the pieces to match the screen and press **Pieces match the
  screen**; the game carries on from that point. Every stage is logged with the prefix
  `PIPELINE`, which `scripts/pi-logs.sh` shows live. The Pi remembers where the physical
  pieces were last left, so a board game started after digital games begins by putting them
  back.

The choice is made per game on the play screen and sent as `"board": true` or `false` with
`/api/new`; the Pi reports it back as `board` in every state. A board game starts without
Stockfish; only the digital game needs it.

`POST /api/board-move {"requestId"?, "promotion"?: "q"|"r"|"b"|"n", "move"?: uci}` is the
board's screen's own route, called on the Pi with the relay token; the website's proxy does not
relay it. It needs a running board game, White to move, an idle board, Online and a camera
(409 `not_board_game`, `not_board_turn`, `board_moving`, `board_fault`, `game_over`,
`board_offline`, `no_camera`; 503 `camera_failed`). It is deduplicated by `requestId` (made
up when absent), and the website player keeps the lease. When the photo does not show exactly
one legal move it answers 409 `board_move_unclear` with a message and the scan's `status`
(`unchanged`, `no_match`, `uncertain` or `ambiguous`), `candidates`, `missing`, `extra` and
`uncertain`. The camera sees only which squares are full: a promotion uses `promotion` (a
queen by default), and a capture that could have been made on more than one square can be
named with `move`, one of the `candidates`. On White's turn the website's `/api/move`
answers 409 `not_your_turn`.

Website games are allowed only while the board's touch screen has **Online** selected (the
fourth option on its menu, from `pi_touch.py` on the `pi-only` side). The screen sends
`POST /api/online {"active": true}` every second while it is; without that heartbeat for
10 s, or after `{"active": false}`, `/api/new`, `/api/move`, `/api/undo` and `/api/board-move` answer 409
`board_offline` ("Tap Online on the board's screen first") and the play screen says so.
Every state carries `online` (whether games are allowed) and `clients` (how many other
sessions talked to the Pi in the last 20 s, which is how the screen knows a player is
connected). Set `CHESS_ONLINE_ALWAYS=1` in `relay.env` to skip the gate on a bench with no
screen. The screen also reads `CHESS_WEBSITE_URL` (the address it shows) and
`CHESS_ACCESS_CODE` (the code it shows) from `relay.env`. When the player presses Start game
while the screen says Online, the play screen preselects **On the board** unless they have
chosen otherwise before.

### How a website game ends

Besides checkmate and the draws, a website game ends in two ways, and never by an idle timer:
an idle tab keeps its game.

* The website reports a clock at zero or a resignation with `POST /api/end {"reason":
  "timeout" | "resignation", "loser": "w" | "b", requestId, expectedGameId,
  expectedRevision}`, which goes through the same deduplication, revision, lease and Online
  checks as `/api/move` (400 `not_started` / `game_over` when there is nothing to end).
* The board's screen presses **End Game / Reset Board**, which sends `POST /api/online
  {"active": true, "reset": true}`: a running game ends with reason `abandoned`.

Every state carries `ended` (null, or `{"reason", "winner", "result", "text"}`), and
`gameOver` is true for either kind of ending: `outcome` becomes `{"result", "winner",
"reason"}` with that reason, and `result` says it in words ("Black wins on time.", "White
wins by resignation.", "Game ended from the board."). Whenever a board game ends, by the
rules, by `/api/end` or from the screen, the Pi clears the lease and queues the reset in the
same transaction: pieces off their home squares are docked, everything is carried home, and
the pipeline's camera stage checks the full starting position (`motion.current` is `reset`
while that runs; the play screen says "The board is putting the pieces back…"). A digital game
only loses its lease. The finished game stays in the state, final position and all, until
the next `/api/new`, which finds the board already set up and plans nothing. A reset from the
screen also clears a stopped board and any steps nobody has done, so it is the only job left.

Once the screen's own camera has verified the starting position it sends `POST /api/online
{"active": true, "fresh": true}`: the record becomes a fresh, unstarted board game with a new
`gameId`, no owner and no queued steps, so the next player sees the pairing screen instead of
the previous player's game, and an open tab of that game gets `stale_state` on its next
command. `reset` and `fresh` together do the reset.

### The gantry slot

The gantry code lives on branch `gantry` of Self-Moving-Chess-Board-Code: ESP32 firmware and
`gantry/pi/gantry_driver.py`, which performs one job from standard input and exits 0 when the
pieces have stopped. `CHESS_GANTRY=arduino` in `relay.env` makes `backend/gantry.py` run it as

```sh
<python> <dir>/gantry_driver.py --port $CHESS_GANTRY_PORT --config <config> --quiet
```

with `CHESS_GANTRY_PORT` the ESP32's serial port (required, such as `/dev/ttyUSB0`),
`<dir>` from `CHESS_GANTRY_DIR` or the first of `~/chess-gantry/gantry/pi` and
`~/Self-Moving-Chess-Board-Code/gantry/pi` that has the program, `<config>` from
`CHESS_GANTRY_CONFIG` or `<dir>/gantry_config.json`, and `<python>` from
`CHESS_GANTRY_PYTHON`, else the pi-only checkout's `~/chess-board-pi-only/.venv/bin/python`
when it exists, else the service's own interpreter. Until a port and the program are both
there, the placeholder stops every board game at its first move and says what is missing.
Board games with nothing configured log each job and carry on, and a motor program in any
language can take the slot through `CHESS_MOTION_COMMAND` in `relay.env`:

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
board only after a gantry has really moved something. It also reads the move the person at the
board makes, so a game on the board cannot get past White's first move without it
(`/api/board-move` answers `no_camera`). `backend/board_camera.py` lists the settings. Restart
`duen-relay` after calibrating.

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
