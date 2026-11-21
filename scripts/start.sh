#!/bin/sh
set -eu
PORT="${PORT:-8080}"
exec uvicorn app:app --host 0.0.0.0 --port "$PORT"
