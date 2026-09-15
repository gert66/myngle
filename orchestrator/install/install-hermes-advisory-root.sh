#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/myngle/orchestrator
WRAP_SRC="$ROOT/install/hermes-orchestrator-advice"
SUDOERS_SRC="$ROOT/install/hermes-orchestrator-advice.sudoers"
WRAP_DST=/usr/local/sbin/hermes-orchestrator-advice
SUDOERS_DST=/etc/sudoers.d/hermes-orchestrator-advice
FLAG="$ROOT/config/hermes_advisory.enabled"

[[ $(id -u) -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -f "$WRAP_SRC" && -f "$SUDOERS_SRC" ]] || { echo "staged files missing" >&2; exit 1; }
getent passwd hermes >/dev/null || { echo "hermes user missing" >&2; exit 1; }
command -v visudo >/dev/null || { echo "visudo missing" >&2; exit 1; }

visudo -cf "$SUDOERS_SRC"
install -o root -g root -m 0755 "$WRAP_SRC" "$WRAP_DST"
install -o root -g root -m 0440 "$SUDOERS_SRC" "$SUDOERS_DST"
visudo -cf "$SUDOERS_DST"
install -o myngle -g myngle -m 0644 /dev/null "$FLAG"
echo "Hermes advisory bridge installed and enabled."
