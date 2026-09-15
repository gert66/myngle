#!/usr/bin/env bash
set -uo pipefail

JOB_RAW="${1:-job}"
MODE="${2:-read}"
PROMPT_FILE="${3:-}"
REPO="${4:-/home/myngle/myngle}"
ACCOUNT_SELECTOR="/home/myngle/orchestrator/bin/select_claude_account.sh"
ACCOUNT_CONF="/home/myngle/orchestrator/config/claude_accounts.conf"
STATE_DIR="/home/myngle/orchestrator/state"
LOG_DIR="/home/myngle/Myngle/Orchestrator Logs"
LOCK_DIR="/home/myngle/orchestrator/locks"
STATUS_DIR="$LOG_DIR"
HEARTBEAT_SEC=60
QUOTA_COOLDOWN_SEC=10800

if [[ -z "$PROMPT_FILE" || ! -f "$PROMPT_FILE" ]]; then
  echo "Usage: $0 <job-name> <read|write> <prompt-file> [repo]" >&2
  exit 2
fi
if [[ "$MODE" != "read" && "$MODE" != "write" ]]; then
  echo "Mode must be read or write" >&2
  exit 2
fi

JOB=$(printf '%s' "$JOB_RAW" | tr ' /:' '---' | tr -cd '[:alnum:]_.-')
STAMP=$(date -u +%Y%m%d-%H%M%S)
LOG="$LOG_DIR/${STAMP}_${JOB}.log"
STATUS="$STATUS_DIR/${STAMP}_${JOB}.status"
CLAUDE_OUT="/tmp/${STAMP}_${JOB}.claude.out"
mkdir -p "$LOG_DIR" "$LOCK_DIR" "$STATE_DIR"
printf "RUNNING\njob=%s\nstarted=%s\n" "$JOB" "$(date -Is)" > "$STATUS"

# Legacy jobs share the same deterministic scheduler and branch-freshness gate
# as phase-3 jobs. The shell PID owns the lease until the trap releases it.
LEGACY_RESULT="ERROR"
release_scheduler_slot() {
  python3 -m core.legacy_slot release --job-id "$JOB" --pid "$$" --result "$LEGACY_RESULT" >/dev/null 2>&1 || true
}
trap release_scheduler_slot EXIT
RESOURCE_CLASS="read"
[[ "$MODE" == "write" ]] && RESOURCE_CLASS="dev"
if ! (cd /home/myngle/orchestrator && python3 -m core.legacy_slot acquire       --job-id "$JOB" --mode "$MODE" --repo-path "$REPO" --pid "$$" --resource-class "$RESOURCE_CLASS"); then
  echo "Legacy job blocked before Claude by scheduler/safety control plane" >&2
  exit 6
fi

quota_limited() {
  local f="$1"
  grep -Eiq 'usage limit|rate limit|rate_limit|quota|capacity limit|too many requests|resets? at|limit reached|you.ve hit your.*limit' "$f" 2>/dev/null
}

mark_cooldown() {
  local name="$1"
  printf '%s\n' "$(( $(date +%s) + QUOTA_COOLDOWN_SEC ))" > "$STATE_DIR/claude_${name}.cooldown_until"
}

git_fingerprint() {
  git -C "$REPO" status --porcelain=v1 -uall 2>/dev/null | sha256sum | awk '{print $1}'
}

pick_account() {
  "$ACCOUNT_SELECTOR"
}

pick_alternate() {
  local exclude="$1" now until name bin prio
  now=$(date +%s)
  while IFS='|' read -r name bin prio; do
    [[ -z "${name:-}" || "$name" == \#* || "$name" == "$exclude" ]] && continue
    [[ -x "$bin" ]] || continue
    "$bin" auth status 2>/dev/null | grep -q '"loggedIn": true' || continue
    until=0
    [[ -f "$STATE_DIR/claude_${name}.cooldown_until" ]] && until=$(cat "$STATE_DIR/claude_${name}.cooldown_until" 2>/dev/null || echo 0)
    [[ "$until" =~ ^[0-9]+$ ]] || until=0
    (( until > now )) && continue
    printf '%s|%s\n' "$name" "$bin"
    return 0
  done < <(grep -v '^#' "$ACCOUNT_CONF" | sort -t'|' -k3,3nr)
  return 1
}

invoke_claude() {
  local account="$1" binary="$2" prompt="$3" outfile="$4"
  : > "$outfile"
  echo "[CLAUDE ACCOUNT] $account"
  "$binary" -p "$prompt" 2>&1 | tee "$outfile" &
  CLAUDE_PID=$!
  START_EPOCH=$(date +%s)
  (
    while kill -0 "$CLAUDE_PID" 2>/dev/null; do
      sleep "$HEARTBEAT_SEC"
      if kill -0 "$CLAUDE_PID" 2>/dev/null; then
        NOW=$(date +%s); ELAPSED=$((NOW-START_EPOCH))
        printf '[HEARTBEAT] %s job=%s account=%s running elapsed=%02dm%02ds\n' "$(date -Is)" "$JOB" "$account" $((ELAPSED/60)) $((ELAPSED%60))
        printf "RUNNING\njob=%s\naccount=%s\nelapsed_seconds=%s\nupdated=%s\n" "$JOB" "$account" "$ELAPSED" "$(date -Is)" > "$STATUS"
      fi
    done
  ) &
  HEARTBEAT_PID=$!
  wait "$CLAUDE_PID"
  local rc=$?
  kill "$HEARTBEAT_PID" 2>/dev/null || true
  wait "$HEARTBEAT_PID" 2>/dev/null || true
  return "$rc"
}

run_job() {
  {
    echo "[JOB] $JOB"
    echo "[MODE] $MODE"
    echo "[REPO] $REPO"
    echo "[START] $(date -Is)"
    echo "[BRANCH] $(git -C "$REPO" branch --show-current 2>/dev/null || true)"
    echo "[STATUS BEFORE]"
    git -C "$REPO" status --short --branch 2>/dev/null || true
    echo
    echo "[CHATGPT -> CLAUDE]"
    cat "$PROMPT_FILE"
    echo
    echo "[CLAUDE -> CHATGPT]"
    cd "$REPO" || exit 3
    AUGMENTED_PROMPT="$(cat "$PROMPT_FILE")

ORCHESTRATOR STATUS RULE: If you cannot proceed or need a human business/technical decision, include a line exactly: [NEEDS_HUMAN] followed by a concise reason. Otherwise do not emit that marker."

    BEFORE_FP=$(git_fingerprint)
    SELECTION=$(pick_account) || { echo "No healthy Claude account available"; return 75; }
    CLAUDE_ACCOUNT=${SELECTION%%|*}
    CLAUDE_BIN=${SELECTION#*|}
    invoke_claude "$CLAUDE_ACCOUNT" "$CLAUDE_BIN" "$AUGMENTED_PROMPT" "$CLAUDE_OUT"
    RC=$?
    if quota_limited "$CLAUDE_OUT"; then
      echo "[ACCOUNT LIMIT] $CLAUDE_ACCOUNT appears quota/rate limited; cooling down for $QUOTA_COOLDOWN_SEC seconds"
      mark_cooldown "$CLAUDE_ACCOUNT"
      AFTER_FP=$(git_fingerprint)
      SAFE_RETRY=false
      [[ "$MODE" == "read" ]] && SAFE_RETRY=true
      [[ "$MODE" == "write" && "$AFTER_FP" == "$BEFORE_FP" ]] && SAFE_RETRY=true
      if [[ "$SAFE_RETRY" == true ]]; then
        ALT=$(pick_alternate "$CLAUDE_ACCOUNT" || true)
        if [[ -n "$ALT" ]]; then
          ALT_ACCOUNT=${ALT%%|*}; ALT_BIN=${ALT#*|}
          echo "[ACCOUNT SWITCH] $CLAUDE_ACCOUNT -> $ALT_ACCOUNT"
          mv "$CLAUDE_OUT" "${CLAUDE_OUT}.first" 2>/dev/null || true
          invoke_claude "$ALT_ACCOUNT" "$ALT_BIN" "$AUGMENTED_PROMPT" "$CLAUDE_OUT"
          RC=$?
          CLAUDE_ACCOUNT="$ALT_ACCOUNT"
        else
          echo "[ACCOUNT SWITCH] no healthy alternate account available"
        fi
      else
        echo "[NEEDS_HUMAN] Claude account limit occurred after repository changes; automatic retry suppressed for safety."
      fi
    fi

    echo
    echo "[STATUS AFTER]"
    git status --short --branch 2>/dev/null || true
    echo "[END] $(date -Is)"
    echo "[EXIT] $RC"
    echo "[FINAL CLAUDE ACCOUNT] $CLAUDE_ACCOUNT"
    if grep -q '\[NEEDS_HUMAN\]' "$CLAUDE_OUT" 2>/dev/null; then
      FINAL_STATUS="NEEDS_HUMAN"
    elif [[ "$RC" -ne 0 ]]; then
      FINAL_STATUS="ERROR"
    else
      FINAL_STATUS="DONE"
    fi
    printf "%s\njob=%s\naccount=%s\nended=%s\nexit=%s\n" "$FINAL_STATUS" "$JOB" "$CLAUDE_ACCOUNT" "$(date -Is)" "$RC" > "$STATUS"
    echo "[ORCHESTRATOR STATUS] $FINAL_STATUS"
    LEGACY_RESULT="$FINAL_STATUS"
    DETAIL=""
    if [[ "$FINAL_STATUS" == "NEEDS_HUMAN" ]]; then
      DETAIL=$(grep -m1 -E '\[NEEDS_HUMAN\]' "$CLAUDE_OUT" 2>/dev/null || true)
    fi
    /home/myngle/orchestrator/bin/notify_slack.sh "$FINAL_STATUS" "$JOB" "$DETAIL" || true
    rm -f "$CLAUDE_OUT" "${CLAUDE_OUT}.first"
    return "$RC"
  } 2>&1 | tee -a "$LOG"
  return ${PIPESTATUS[0]}
}

if [[ "$MODE" == "write" ]]; then
  LOCK="$LOCK_DIR/$(printf '%s' "$REPO" | tr '/' '_').lock"
  exec 9>"$LOCK"
  if ! flock -n 9; then
    echo "Another write job is already active for $REPO" >&2
    exit 4
  fi
fi

run_job
RC=$?
echo "$LOG"
exit "$RC"