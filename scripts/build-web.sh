#!/usr/bin/env bash
set -euo pipefail
mkdir -p public
cp index.html concept.html play.html app.js chess.js style.css landing.css concept.css landing.js how-it-works.js public/
cp -R assets public/
