#!/usr/bin/env bash
set -uo pipefail

STATUS="${1:-}"
JOB="${2:-unknown-job}"
DETAIL="${3:-}"
SECRET_FILE="/home/myngle/orchestrator/secrets/slack_webhook_url"

if [[ ! -s "$SECRET_FILE" ]]; then
  echo "[NOTIFY] Slack not configured; skipped ($STATUS $JOB)"
  exit 0
fi

WEBHOOK_URL=$(cat "$SECRET_FILE")
case "$STATUS" in
  NEEDS_HUMAN) PREFIX="ACTION NEEDED" ;;
  ERROR) PREFIX="ERROR" ;;
  DONE) PREFIX="DONE" ;;
  *) PREFIX="$STATUS" ;;
esac

TEXT="Orchestrator: $PREFIX\nJob: $JOB"
if [[ -n "$DETAIL" ]]; then TEXT="$TEXT\n$DETAIL"; fi
PAYLOAD=$(python3 -c 'import json,sys; print(json.dumps({"text": sys.stdin.read()}))' <<< "$TEXT")

HTTP_CODE=$(curl -sS -o /tmp/orchestrator-slack-response.txt -w '%{http_code}' \
  -H 'Content-Type: application/json' -d "$PAYLOAD" "$WEBHOOK_URL" || true)

if [[ "$HTTP_CODE" == "200" ]]; then
  echo "[NOTIFY] Slack sent ($STATUS $JOB)"
  exit 0
fi

echo "[NOTIFY] Slack failed HTTP=$HTTP_CODE ($STATUS $JOB)" >&2
exit 1
