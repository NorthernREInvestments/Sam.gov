#!/bin/sh
set -eu
cd /app/public
PORT="${PORT:-8080}"
exec python -m http.server "$PORT"
