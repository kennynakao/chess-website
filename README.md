# Chess Website

A browser chess board inspired by the play screen on chess.com. Plain HTML, CSS and JavaScript with no build step and no dependencies.

**Live site:** https://kennynakao.github.io/chess-website/

## Features

- Full chess rules: legal move generation, castling, en passant, promotion, check, checkmate, stalemate, threefold repetition, 50-move rule and insufficient material
- Click-to-move and drag-and-drop with legal move hints, last-move and check highlights
- Clocks with the usual bullet / blitz / rapid presets and increments, or unlimited time
- Play against a casual computer opponent (random mover) or pass-and-play with a friend
- Move list in algebraic notation with navigation (buttons or arrow keys)
- Captured pieces and material count, flip board, takeback, resign, draw, rematch
- Game-over popup, PGN and FEN export, synthesized move sounds
- Responsive layout for desktop and phone

## Run locally

Open `index.html` in a browser, or serve the folder:

```bash
python3 -m http.server 8080
```

Then visit http://localhost:8080.

## Tests

The rules engine has a perft test suite (move counts from known positions) plus rule checks:

```bash
node test/perft.js
```

## Files

| File | Purpose |
| --- | --- |
| `index.html` | Page structure |
| `style.css` | Layout and theme |
| `chess.js` | Rules engine (move generation, SAN, FEN, PGN) |
| `app.js` | Board UI, clocks, move list, game flow |
| `assets/pieces/` | Piece graphics |
| `test/perft.js` | Engine tests |

## Credits

Piece graphics are the "cburnett" set by Colin M. L. Burnett, used under the GPLv2+ / CC BY-SA 3.0 licenses.

## License

MIT
