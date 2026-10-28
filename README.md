# DUEN Chess — remote Raspberry Pi play

Website: https://chess-website-gules.vercel.app

## How a move travels

Browser → Vercel API → authenticated HTTPS tunnel → Raspberry Pi → Stockfish → browser.
The Pi validates both moves and saves the game before returning a reply. The Mac
is only a development/deployment tool; it is not in the live connection path.

The Pi must remain powered on and online. No router port forwarding is needed.
The current integration runs chess logic and Stockfish; it does not drive motors.

## Repository layout

- `index.html`, `landing.js`, `landing.css`: the animated landing page.
- `play.html`, `app.js`, `chess.js`, `style.css`: the chess interface.
- `assets/vendor/`: the locally served Three.js runtime and its license.
- `api/[action].js`: Vercel login/session handling and server-only Pi proxy.
- `backend/remote_server.py`: loopback-only authenticated Pi API and SQLite state.
- `backend/chess_game.py`: shared rules and Stockfish integration.
- `scripts/`: static build, Pi deployment, and live logs.
- `test/`: rules, premoves, remote client, backend, and proxy checks.

## Credentials

Vercel production environment variables:

- `PI_API_URL`: stable HTTPS URL of the Pi's tunnel (origin only).
- `CHESS_RELAY_TOKEN`: random 32+ character server-to-server token.
- `CHESS_SESSION_SECRET`: random key used to sign browser sessions.
- `CHESS_ACCESS_CODE`: private board access code entered on the website.

The Pi stores only `CHESS_RELAY_TOKEN` and `CHESS_STATE_PATH` in
`~/.config/duen-chess/relay.env` (mode 600). Never commit these values.
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

## Validation

```sh
node test/perft.js
node test/premove.js
node test/remote-client.js
node --test test/proxy.test.js
python -m unittest discover -s test -p 'test_remote_server.py'
```

Vercel builds `public/` with `scripts/build-web.sh` and deploys only the static
landing page, chess interface, and the `api/` function. The landing page links
to `/play.html`; the existing Pi connection remains on that game page.

The landing scene uses a dark-purple backdrop, a floating wooden pawn, and a
scroll-driven opening move. It pauses rendering when settled or out of view,
respects reduced motion, and caps its drawing buffer on large displays.
