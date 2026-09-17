#!/bin/sh
set -eu
ROOT=/home/myngle/orchestrator
LOG="$ROOT/logs/feedback-messages-service.log"
HEARTBEAT="$ROOT/state/feedback-message-poller.heartbeat"
LOCK=/tmp/sales-cockpit-feedback-messages.lock
DAEMON_LOCK=/tmp/sales-cockpit-feedback-messages-daemon.lock
exec 9>"$DAEMON_LOCK"
/usr/bin/flock -n 9 || exit 0
mkdir -p "$ROOT/logs" "$ROOT/state"
while true; do
  if /usr/bin/flock -n "$LOCK" /usr/bin/python3 "$ROOT/bin/process_feedback_messages.py" >> "$LOG" 2>&1; then
    touch "$HEARTBEAT"
  fi
  sleep 5
done
