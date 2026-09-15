#!/usr/bin/env bash
set -euo pipefail

URL="${1:-}"
NAME="${2:-crawl-$(date -u +%Y%m%dT%H%M%SZ)}"
LOCK="/home/myngle/orchestrator/locks/crawl4ai.lock"
OUTDIR="/srv/orchestrator-export/crawl4ai"
OUT="$OUTDIR/${NAME}.json"
PY="/home/myngle/orchestrator/crawl4ai-worker/.venv/bin/python"
WORKER="/home/myngle/orchestrator/crawl4ai-worker/crawl_once.py"
BROWSERS="/home/myngle/orchestrator/crawl4ai-worker/ms-playwright-links"

[[ -n "$URL" ]] || { echo "Usage: $0 <https-url> [name]" >&2; exit 2; }
mkdir -p "$OUTDIR" /home/myngle/orchestrator/locks
setfacl -m u:hermes:rx "$OUTDIR"

exec 9>"$LOCK"
flock -n 9 || { echo "Crawl4AI worker already busy" >&2; exit 75; }

export PLAYWRIGHT_BROWSERS_PATH="$BROWSERS"
/usr/bin/time -v timeout --kill-after=10s 60s "$PY" "$WORKER" "$URL" "$OUT"
setfacl -m u:hermes:r "$OUT"
chmod 640 "$OUT"
printf '%s\n' "$OUT"
