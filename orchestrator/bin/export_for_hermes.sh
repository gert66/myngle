#!/usr/bin/env bash
set -euo pipefail

SRC="/home/myngle/orchestrator/jobs"
DEST="/srv/orchestrator-export"
ALLOWLIST=(events.jsonl calls.jsonl state.json job.json)

mkdir -p "$DEST"

for job_dir in "$SRC"/*/; do
  [[ -d "$job_dir" ]] || continue
  job_name="$(basename "$job_dir")"
  out_dir="$DEST/$job_name"
  mkdir -p "$out_dir"

  for f in "${ALLOWLIST[@]}"; do
    if [[ -f "$job_dir$f" ]]; then
      install -m 640 "$job_dir$f" "$out_dir/$f"
    fi
  done
done

# Grant only the isolated Hermes user read/traverse access.
setfacl -m u:hermes:rx "$DEST"
find "$DEST" -type d -exec setfacl -m u:hermes:rx {} +
find "$DEST" -type f -exec setfacl -m u:hermes:r {} +
