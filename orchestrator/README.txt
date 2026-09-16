Orchestrator setup

Logs and status files:
/home/myngle/Myngle/Orchestrator Logs
These sync to Nextcloud/SURFdrive every 2 minutes.

Runner:
/home/myngle/orchestrator/bin/run_claude_job.sh

Usage:
run_claude_job.sh <job-name> <read|write> <prompt-file> [repo]

Every log records job name, mode, repo, start/end time, branch,
Git status before/after, ChatGPT->Claude prompt, Claude->ChatGPT output,
and exit status.

While Claude runs, a heartbeat is printed every 60 seconds.
A matching .status file shows RUNNING, DONE, ERROR or NEEDS_HUMAN.
Claude is instructed to emit NEEDS_HUMAN when a human decision blocks progress.

Write jobs use a per-repo lock, so two orchestrator write jobs cannot
run at the same time in the same repo. Read jobs can run in parallel.

Durable intake protocol:
For any ChatGPT request that should become an orchestrator job, persist an intake receipt FIRST,
before planning or long analysis. Use `python3 -m core.intake receive ...`.
Only after a real job has been created should the receipt be updated to SUBMITTED with its job_id.
`orchestrator-intake-watch.timer` checks every 5 minutes and alerts once when a RECEIVED receipt
is still not submitted after 10 minutes. This prevents interrupted ChatGPT turns from failing silently.

Delegation status contract:
- ChatGPT must create a durable intake receipt before saying that delegated work is underway.
- RECEIVED means HANDOFF: accepted, but no worker job is running yet.
- SUBMITTED is allowed only after a real orchestrator job_id exists.
- Orchestrator Control is the authoritative view for delegated VM work; ChatGPT sidebar activity is not a VM-job signal.
- A run counts as RUNNING only while it is in an active phase and its supervisor heartbeat is fresh.
- Supervisors write heartbeat.json every 15 seconds. Orchestrator Control marks an active phase stale after 75 seconds without a healthy heartbeat.
- DONE, ERROR and NEEDS_HUMAN are notified once through the configured Slack hook.
- The snapshot exposes handoffs, live runs, attention, the five most recent completions, and history separately.
