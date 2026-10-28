#!/usr/bin/env bash
set -euo pipefail
mkdir -p public
cp index.html play.html app.js chess.js style.css landing.css landing.js public/
cp -R assets public/
