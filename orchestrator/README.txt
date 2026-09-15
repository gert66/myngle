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
