#!/usr/bin/env bash
set -euo pipefail
mkdir -p public
cp index.html app.js chess.js style.css public/
cp -R assets public/
