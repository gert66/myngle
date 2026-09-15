#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/myngle/orchestrator"
SNAP="/home/myngle/Myngle/Orchestrator Logs/ops_snapshot.json"
TOKEN_FILE="$ROOT/secrets/orchestrator_ingest_token"
URL_FILE="$ROOT/config/ops_push_url.txt"
LOG="$ROOT/logs/ops-push.log"

[[ -s "$SNAP" && -s "$TOKEN_FILE" && -s "$URL_FILE" ]] || exit 0
TOKEN=$(cat "$TOKEN_FILE")
URL=$(cat "$URL_FILE")
TS=$(date -u +%FT%TZ)

HTTP=$(curl -sS -o /tmp/orchestrator_ops_push_response.json -w '%{http_code}' \
  -X POST "$URL" \
  -H "x-orchestrator-token: $TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary "@$SNAP" || true)

printf '%s http=%s\n' "$TS" "$HTTP" >> "$LOG"
[[ "$HTTP" == "200" ]]
