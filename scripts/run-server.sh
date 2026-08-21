#!/usr/bin/env bash
# Supervised local server.
#
# `uv run python -m src &` has zero restart semantics: when the process died
# mid-triage (a native abort in the Gmail thread pool left no traceback at all)
# it simply stayed dead and every request failed until a human noticed.
#
# This wrapper restarts the server whenever it exits non-zero, logs every
# restart with a timestamp, and backs off so a boot-time crash loop does not
# spin the CPU. On boot the API already reconciles orphaned `running` triage
# runs (`src/api/__init__.py:_reconcile_orphaned_runs`) — the supervisor is what
# makes that fire automatically instead of waiting for a manual restart.
#
# Usage:
#   ./scripts/run-server.sh            # foreground, Ctrl-C to stop
#   MAX_RESTARTS=0 ./scripts/run-server.sh   # unlimited restarts (default 100)
#
# Stop with Ctrl-C (SIGINT/SIGTERM are forwarded and end the loop — a clean
# shutdown is never restarted).

set -uo pipefail

cd "$(dirname "$0")/.."

MAX_RESTARTS="${MAX_RESTARTS:-100}"
BACKOFF_SECONDS="${BACKOFF_SECONDS:-2}"
LOG_PREFIX="[supervisor]"

child_pid=""
stopping=0

_shutdown() {
  stopping=1
  if [[ -n "$child_pid" ]]; then
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  echo "$LOG_PREFIX $(date -u +%FT%TZ) stopped by signal"
  exit 0
}
trap _shutdown INT TERM

restarts=0
while true; do
  echo "$LOG_PREFIX $(date -u +%FT%TZ) starting (restart #$restarts)"
  # PYTHONFAULTHANDLER is belt-and-braces alongside faulthandler.enable() in
  # src/__main__.py: a native abort must always leave a stack behind.
  PYTHONFAULTHANDLER=1 uv run python -m src &
  child_pid=$!
  wait "$child_pid"
  status=$?
  child_pid=""

  if [[ "$stopping" -eq 1 ]]; then
    exit 0
  fi
  # 78 = os.EX_CONFIG, raised by src/__main__.py for an unrecoverable configuration
  # error (e.g. a missing AGENT_SECRET_KEY). Restarting cannot fix it, and a
  # readable banner repeated 100 times is still an unreadable crash loop.
  if [[ "$status" -eq 78 ]]; then
    echo "$LOG_PREFIX $(date -u +%FT%TZ) fatal configuration error (78) — not restarting" >&2
    exit 78
  fi
  if [[ "$status" -eq 0 ]]; then
    echo "$LOG_PREFIX $(date -u +%FT%TZ) exited cleanly (0) — not restarting"
    exit 0
  fi

  restarts=$((restarts + 1))
  echo "$LOG_PREFIX $(date -u +%FT%TZ) server died with status $status — restarting in ${BACKOFF_SECONDS}s (restart $restarts)" >&2
  if [[ "$MAX_RESTARTS" -ne 0 && "$restarts" -ge "$MAX_RESTARTS" ]]; then
    echo "$LOG_PREFIX $(date -u +%FT%TZ) giving up after $restarts restarts" >&2
    exit "$status"
  fi
  sleep "$BACKOFF_SECONDS"
done
