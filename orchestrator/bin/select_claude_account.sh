#!/usr/bin/env bash
set -uo pipefail
CONF="/home/myngle/orchestrator/config/claude_accounts.conf"
STATE_DIR="/home/myngle/orchestrator/state"
NOW=$(date +%s)
BEST=""
BEST_BIN=""
BEST_PRIO=-1
while IFS='|' read -r NAME BIN PRIO; do
  [[ -z "${NAME:-}" || "$NAME" == \#* ]] && continue
  [[ -x "$BIN" ]] || continue
  if ! "$BIN" auth status 2>/dev/null | grep -q '"loggedIn": true'; then
    continue
  fi
  COOLDOWN_FILE="$STATE_DIR/claude_${NAME}.cooldown_until"
  if [[ -f "$COOLDOWN_FILE" ]]; then
    UNTIL=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)
    [[ "$UNTIL" =~ ^[0-9]+$ ]] || UNTIL=0
    if (( UNTIL > NOW )); then
      continue
    fi
    rm -f "$COOLDOWN_FILE"
  fi
  if (( PRIO > BEST_PRIO )); then
    BEST="$NAME"; BEST_BIN="$BIN"; BEST_PRIO="$PRIO"
  fi
done < "$CONF"
[[ -n "$BEST" ]] || exit 1
printf '%s|%s\n' "$BEST" "$BEST_BIN"
