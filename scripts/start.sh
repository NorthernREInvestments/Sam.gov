#!/bin/sh
set -eu
PORT=""
exec uvicorn app:app --host 0.0.0.0 --port ""
