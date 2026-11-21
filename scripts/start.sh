#!/bin/sh
set -eu

# One-time wipe of legacy application files on the attached volume.
# Marker prevents repeat deletes after the rebuild stores new data under /data.
if [ -d /data ]; then
  if [ -f /data/.m3_rebuild_cleared_v2 ]; then
    echo "M3_DATA_WIPE: already cleared (marker present)"
  else
    echo "M3_DATA_WIPE: starting"
    du -sh /data 2>/dev/null || true
    ls -la /data 2>/dev/null || true
    # Delete contents; keep the mount point
    find /data -mindepth 1 -maxdepth 1 ! -name '.m3_rebuild_cleared_v2' -print -exec rm -rf {} + 2>&1 || true
    : > /data/.m3_rebuild_cleared_v2
    echo "M3_DATA_WIPE: done"
    du -sh /data 2>/dev/null || true
    ls -la /data 2>/dev/null || true
  fi
else
  echo "M3_DATA_WIPE: /data not mounted"
fi

cd /app/public
PORT="${PORT:-8080}"
exec python -m http.server "$PORT"

