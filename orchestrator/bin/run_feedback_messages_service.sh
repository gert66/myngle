#!/bin/sh
set -eu
ROOT=/home/myngle/orchestrator
LOG="$ROOT/logs/feedback-messages-service.log"
HEARTBEAT="$ROOT/state/feedback-message-poller.heartbeat"
LOCK=/tmp/sales-cockpit-feedback-messages.lock
mkdir -p "$ROOT/logs" "$ROOT/state"
while true; do
  if /usr/bin/flock -n "$LOCK" /usr/bin/python3 "$ROOT/bin/process_feedback_messages.py" >> "$LOG" 2>&1; then
    touch "$HEARTBEAT"
  fi
  sleep 5
done
