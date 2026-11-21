#!/bin/sh
set -eu

# One-time wipe of legacy application files on the attached volume.
# Marker prevents repeat deletes after the rebuild stores new data under /data.
if [ -d /data ] && [ ! -f /data/.m3_rebuild_cleared ]; then
  find /data -mindepth 1 -maxdepth 1 ! -name '.m3_rebuild_cleared' -exec rm -rf {} + 2>/dev/null || true
  : > /data/.m3_rebuild_cleared
fi

cd /app/public
PORT="${PORT:-8080}"
exec python -m http.server "$PORT"
