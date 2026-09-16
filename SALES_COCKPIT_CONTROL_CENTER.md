# Sales Cockpit Control Center

Status: canonical architecture and rollout plan
Branch: `work`
Date: 2026-09-16

## Goal

One mobile-first control app for the complete Sales Cockpit operation. It should replace Slack as the primary approval and feedback interface and gradually reduce the need to operate through ChatGPT for routine Sales Cockpit work.

The existing Lovable project **Orchestrator Watch** should be evolved rather than creating a second app. It already provides authentication, mobile-first monitoring, and a working read-only connection to the VM.

## Core principle

The owner should see decisions, conversations and operational state. Technical implementation details remain available on demand but stay out of the default interface.

Default interaction model:

`incoming feedback -> autonomous investigation -> prepared fix -> owner decision -> execution -> verification -> reporter reply`
## Primary mobile navigation

Keep the app to four primary destinations:

1. **Today**: what is running, what needs a decision, what was resolved, system warnings.
2. **Inbox**: feedback and approvals, ordered by whether Gert needs to act.
3. **Chat**: one continuous operational conversation with the Sales Cockpit agent.
4. **System**: jobs, VM health, queues, capacity and deployment state.

No separate technical navigation for normal use. Deep details open from a card only when requested.

## Approval card

Default card should fit on roughly one phone screen:

- reporter + company
- original report
- agent conclusion
- prepared fix
- proposed reply
- Approve / Discuss / Reject

Resolution note, changed files, tests, proposal fingerprint, logs and diff remain behind a `Details` action.
## Chat model

Chat is the front door for operational commands. Examples:

- `What is running?`
- `Take care of Carla's feedback.`
- `Why did Crawl4A stop?`
- `Approve the prepared fix.`
- `Check HubSpot first and come back to me.`

Each message becomes either a read-only question, an orchestrator intake receipt, an approval action, or a controlled write command. Long-running work must immediately create a durable receipt so the app can show that it is genuinely running.

The conversation persists independently from any single model session. The UI shows explicit states such as `working`, `waiting for you`, `done`, and `failed` rather than relying on chat text promises.

## Backend architecture

The current `ops_api.py` remains the status/read layer. It is currently authenticated and read-only, which is a good safety boundary.

Add a separate authenticated **Control API** for writes. Never expose arbitrary shell execution from the browser.

Control API commands should be limited typed operations such as:

- create orchestrator intake/job
- approve/reject/request-discussion for an exact proposal fingerprint
- retry/cancel an eligible job
- acknowledge an alert
- submit a chat message
- request a diagnostic or safe maintenance action

Every command returns a command id, is idempotent, and is written to an audit log before execution.
## Data model

Use Supabase for app-facing persistence and realtime updates, with the VM/orchestrator remaining authoritative for execution state.

Suggested entities:

- `conversations` and `messages`
- `feedback_items`
- `proposals`
- `commands`
- `job_mirrors`
- `notifications`
- `audit_events`

The app should never need direct filesystem access. A small sync/service layer mirrors sanitized orchestrator state into Supabase or serves it through the existing authenticated status API.

## Notifications

Primary notification should eventually be a phone push from the Control Center, not Slack. Slack remains a fallback channel during rollout.

Only notify for:

- owner decision required
- important failure or unhealthy system state
- optional completion of owner-requested work

Routine progress stays visible in the app without generating notifications.
## Current implementation baseline

Already available on `work`:

- trustworthy orchestrator heartbeat/handoff status;
- unified activity records for orchestrator plus registered ChatGPT/tool work;
- Slack approval gate with exact proposal fingerprint binding;
- approval records projected into the sanitized orchestrator snapshot for the Control Center;
- existing Orchestrator Watch Lovable app with authentication and VM snapshot ingest.

The Control Center is now the canonical UI project. Separate status/interfacing and feedback-automation UI work should not diverge into separate apps.

## Rollout

### Phase 1: consolidate UI

Evolve Orchestrator Watch into Sales Cockpit Control Center. Add Today and Inbox, surface the existing feedback approvals there, keep existing Live/History/Attention/Capacity information available under System.

Slack remains active as fallback. No new autonomous write permissions beyond the current approval gate.

### Phase 2: controlled actions

Add the Control API and app actions. Approve/Discuss/Reject moves from Slack to the app. Add retry/cancel where the orchestrator contract explicitly permits it. Add durable app-side command status.

### Phase 3: operational chat

Add the persistent Chat view. Route user messages into read-only queries or durable orchestrator intake. The agent prepares work and returns decisions to the same conversation.

### Phase 4: app becomes primary interface

Add push notifications and mature the agent/tool layer. Slack becomes fallback only. Routine Sales Cockpit operation no longer requires opening ChatGPT, while exceptional/general-purpose work can still be done there.

## Safety boundaries

- `main` remains protected; routine work targets `work`.
- approval is bound to exact proposal version/fingerprint.
- destructive, permission, bulk-data and security changes require explicit approval.
- browser never receives VM secrets or arbitrary command execution capability.
- every write command is authenticated, authorized, idempotent and audited.
- UI must distinguish `queued`, `working`, `waiting for you`, `executing`, `verified`, `failed`.

## Product test

The app succeeds when Gert can open it on his phone and answer three questions within seconds:

1. Does anything need me?
2. What is the system doing right now?
3. Can I tell it what to do next from here?

Everything else is secondary detail.
