# Sales Cockpit Feedback Autopilot

Status: implementation specification
Branch: `work`
Scope: Sales Cockpit feedback intake, autonomous triage, safe repair, deployment verification, resolution notes, and human escalation.

## 1. Goal

Feedback from callers should normally flow from report to resolution without Gert acting as the router.

The autopilot must:

1. receive feedback from Slack/webhook;
2. collect enough context to understand the report;
3. classify the request and estimate confidence and change risk;
4. investigate and reproduce where practical;
5. autonomously fix low-risk issues;
6. test the change;
7. commit only to `gert66/myngle` branch `work`;
8. deploy only through the existing approved deployment route;
9. verify the production result;
10. write a useful internal resolution note;
11. send a short, human reply to the reporter;
12. escalate to Gert only when a real decision or elevated risk remains.

The system must never merge to `main` autonomously.

## 2. Operating principle

Autonomy is determined by risk, confidence, and evidence, not by the number of changed lines.

A one-line change to copy can be low risk. A one-line change to account ownership, permissions, HubSpot writes, scoring, or data deletion can be high risk.

Every autonomous action must be reconstructable afterwards.

## 3. Input contract

Recommended webhook payload:

```json
{
  "feedback_id": "fb_...",
  "created_at": "2026-09-16T08:00:00+02:00",
  "reporter": {
    "name": "Carla",
    "slack_user_id": "...",
    "email": "optional"
  },
  "message": "Could not log the outcome when I choose ...",
  "workspace": "Sales Cockpit",
  "country": "Germany",
  "company": "optional",
  "page_url": "optional",
  "route": "optional",
  "browser": "optional",
  "attachments": [
    {
      "type": "image",
      "url": "..."
    }
  ],
  "source": "slack"
}
```

The webhook handler should enrich this with system context when available:

- current deployed revision;
- relevant app/module;
- authenticated caller identity;
- recent error/log correlation ID;
- related previous feedback;
- current code SHA on `work`;
- production SHA/revision.

## 4. State machine

Each feedback item moves through explicit states:

`received -> triaging -> investigating -> classified -> fixing -> testing -> ready_to_deploy -> deploying -> verifying -> resolved`

Alternative terminal/intermediate states:

- `needs_reporter_info`
- `needs_gert_decision`
- `blocked_external`
- `failed_test`
- `failed_deploy`
- `verification_failed`
- `duplicate`
- `not_a_bug`

No item may jump from `received` directly to `resolved` unless it is a confirmed duplicate or clearly informational.

## 5. Two independent scores

### Understanding confidence

`0.00 - 1.00`

Questions:

- Is the requested behavior unambiguous?
- Is there enough context to identify the affected flow?
- Can the likely bug be reproduced or otherwise evidenced?
- Is the expected behavior known from existing product behavior/tests?

Suggested thresholds:

- `>= 0.85`: high confidence
- `0.65 - 0.84`: investigate further before changing anything
- `< 0.65`: request more information or escalate

### Change risk

`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`

Risk considers blast radius, reversibility, data mutation, permissions, external systems, deployment impact, and whether the expected behavior is already encoded in tests/specs.

## 6. Autonomy levels

### GREEN

Autopilot may investigate, modify, test, commit to `work`, deploy using the approved route, verify, resolve, and answer the reporter.

Requirements:

- understanding confidence >= 0.85;
- change risk LOW;
- expected behavior can be established;
- relevant tests pass;
- production verification succeeds;
- no protected-system rule is triggered.

Typical examples:

- broken link or redirect;
- `mailto:` opening the wrong client where expected behavior is already defined;
- copy/label issue;
- simple form validation bug;
- missing guard causing a deterministic UI error;
- styling/layout regression with isolated scope;
- broken button handler;
- deterministic logging failure with no schema/data migration.

### AMBER

Autopilot performs investigation, reproduction, root-cause analysis, code change if useful, and tests. It stops before the risky action and asks Gert for a focused decision.

Typical triggers:

- change risk MEDIUM;
- product behavior has two plausible interpretations;
- multiple modules are affected;
- deployment has a wider blast radius;
- external API behavior is uncertain;
- migration/backfill is required;
- a fix changes business logic rather than restoring existing behavior.

Escalation must be specific:

- what was reported;
- what was found;
- exact proposed change;
- why the system stopped;
- one concrete decision needed from Gert.

### RED

Autopilot investigates and produces a recommendation only. No write, deploy, data mutation, or user-facing closure.

Triggers include:

- destructive data operation;
- HubSpot ownership or assignment changes with material impact;
- authentication/authorization or role changes;
- security/privacy issue;
- secrets or credentials;
- billing/payment;
- production database/schema migration with risk;
- bulk update/delete;
- irreversible external-system action;
- uncertainty about whether user data may be overwritten;
- change to deployment infrastructure or protected branch policy;
- anything involving `main` merge without explicit approval.

## 7. Protected actions

The following always require explicit approval unless a narrower policy is later defined:

- merge to `main`;
- delete production data;
- bulk modify HubSpot records;
- alter permissions/admin roles;
- rotate secrets;
- alter ownership allocation rules;
- change billing/subscription behavior;
- schema migrations that are not trivially backward-compatible;
- disable audit/logging safeguards;
- change autonomous risk thresholds themselves.

## 8. Investigation pipeline

For every non-trivial report:

1. normalize the feedback text;
2. identify likely surface and module;
3. inspect screenshot/attachment if supplied;
4. find related logs/errors;
5. inspect current code on `gert66/myngle`, branch `work`;
6. check whether the report matches a recent deployment/regression;
7. search for related tests and previous feedback;
8. formulate one or more hypotheses;
9. attempt a safe reproduction;
10. record evidence supporting the chosen root cause.

The system must distinguish:

- confirmed root cause;
- strongly supported hypothesis;
- unresolved suspicion.

Only the first two can proceed autonomously, and a strongly supported hypothesis must still satisfy GREEN risk rules.

## 9. Fix policy

For GREEN fixes:

- make the smallest coherent change;
- add or update a regression test whenever practical;
- avoid unrelated refactoring;
- preserve existing interfaces unless the bug requires otherwise;
- never use the report itself as sufficient proof that the proposed fix is correct;
- use the current `work` branch as the base;
- do not create a new branch when `work` exists unless explicitly required.

Commit message format:

`fix(feedback): <short concrete description>`

The audit record stores the resulting commit SHA.

## 10. Test gate

A GREEN item cannot deploy until all applicable gates pass.

Minimum:

- targeted regression test;
- relevant existing test subset;
- lint/type/build checks used by that component;
- smoke test for the affected flow where possible.

For UI issues:

- verify the affected interaction, not only successful build output.

For external integrations:

- prefer a read-only or sandbox verification first;
- never treat an HTTP 200 alone as proof that the business outcome is correct.

If unrelated known flaky tests fail, record them explicitly. Do not silently ignore new failures.

## 11. Deployment gate

Autonomous deployment is allowed only for GREEN items and only using the existing approved Sales Cockpit deployment mechanism.

Before deployment, record:

- pre-deploy production revision;
- commit SHA being deployed;
- test results;
- rollback target.

After deployment:

- confirm the new revision is active;
- run the same affected-flow smoke check;
- inspect new errors for the relevant route/function;
- compare expected vs observed behavior.

If verification fails:

1. mark `verification_failed`;
2. rollback automatically only when the existing deployment mechanism has a proven safe rollback route;
3. otherwise stop and escalate;
4. never tell the reporter the issue is fixed.

## 12. Reporter communication

The autopilot should reply only after production verification, unless it needs additional information.

Style:

- short;
- personal;
- mention the concrete issue;
- thank the reporter;
- say what changed in normal language;
- do not expose implementation details unless useful;
- do not claim success before verification.

Example resolved message:

> Hi Carla, thanks for flagging this. You were right, there was an issue when saving some call outcomes. We fixed it and checked the flow again in the Sales Cockpit. Thanks for catching it.

Example request for information:

> Hi Carla, thanks for reporting this. I can see the area you mean, but I cannot reproduce the error yet. Could you tell me which outcome you selected when it happened? That should be enough for me to trace it further.

The reporter message and internal resolution note are separate artifacts.

## 13. Internal resolution note

Required fields:

```yaml
feedback_id: fb_...
reporter: Carla
classification: bug
risk_level: LOW
autonomy: GREEN
understanding_confidence: 0.94
status: resolved
root_cause: "..."
change_summary: "..."
files_changed:
  - path/to/file.py
tests:
  - "... PASS"
commit_sha: "..."
deployment_revision: "..."
production_verification: "..."
rollback_used: false
reporter_reply: "..."
resolved_at: "..."
```

Human-readable note example:

> Confirmed a bug in call-outcome saving for a specific selection path. The handler passed an empty value into the logging request, which caused the save to fail. Added a guard and regression test, deployed the fix, and re-tested the affected flow in production. No HubSpot data model or existing records were changed.

## 14. Audit log

Every feedback item should have an append-only event history.

Event examples:

- `feedback_received`
- `triage_completed`
- `reproduction_succeeded`
- `root_cause_confirmed`
- `risk_classified`
- `code_changed`
- `tests_passed`
- `commit_created`
- `deploy_started`
- `deploy_succeeded`
- `production_verified`
- `reporter_notified`
- `escalated_to_gert`

Each event stores timestamp, actor/model, code SHA where applicable, and a compact evidence/result field.

## 15. Slack/webhook behavior

Recommended architecture:

`Slack -> webhook endpoint -> feedback queue -> triage worker -> orchestrator -> action runner -> deployment verifier -> Slack reply + feedback store`

The webhook endpoint should acknowledge quickly and place work onto a durable queue. The Slack request itself should not wait for investigation or deployment.

Idempotency key:

`source + source_event_id`

This prevents duplicate Slack delivery from creating duplicate fixes.

## 16. Model routing

Use the orchestrator rather than one model for the whole job.

### Low-cost first pass

Gemini Flash can handle:

- normalization;
- classification;
- extraction of reproduction clues;
- likely module selection;
- duplicate detection;
- resolution-note drafting;
- reporter-message drafting;
- summarizing test output.

### Stronger reasoning/code route

Escalate model strength when:

- confidence remains below threshold;
- root cause spans modules;
- tests and code contradict the report;
- the patch is non-obvious;
- external integration semantics matter;
- a security/data issue is suspected.

Model choice must never lower the GREEN/AMBER/RED safety thresholds.

## 17. Automatic escalation rules

Escalate to Gert when any of these are true:

- confidence remains < 0.85 after investigation;
- risk is MEDIUM or higher;
- expected product behavior is ambiguous;
- reporter asks for a feature rather than restoration of expected behavior;
- fixing one user breaks another valid workflow;
- test failures cannot be explained;
- deployment or production verification fails;
- HubSpot schema, ownership, permissions, or bulk writes are involved;
- data deletion/backfill is proposed;
- security/privacy may be involved;
- the system would need to merge to `main`;
- the same feedback has failed autonomous repair twice.

Escalation should not ask Gert to re-investigate the whole case. It should bring the investigation result plus the smallest decision needed.

## 18. Retry policy

Autopilot gets at most two autonomous repair attempts for the same feedback item.

After the first failed fix:

- reassess the hypothesis;
- do not simply retry the same patch;
- raise risk by one level unless the failure was clearly infrastructural.

After the second failed repair attempt:

- mandatory escalation;
- include both attempted hypotheses and evidence.

## 19. Duplicate feedback

Before investigation, compare the report against open/recent feedback using:

- normalized error text;
- route/module;
- reporter context;
- semantic similarity;
- correlation/log signature.

If duplicate:

- attach it to the canonical feedback item;
- preserve the additional reporter/context;
- do not launch a second repair job;
- notify the new reporter after the canonical issue is verified fixed.

## 20. Feature request vs bug

Autopilot may autonomously repair behavior only when it is restoring a reasonably established expected behavior.

Possible feature requests should be classified separately.

Examples:

- "This button used to open Outlook and now opens Apple Mail" may be a regression if the expected route is established.
- "I would prefer this button to open HubSpot instead" is a product choice if no expected behavior was previously established.

Feature requests can be researched and scoped automatically but normally enter AMBER for product approval.

## 21. Minimum persistence model

Suggested tables/collections:

### `feedback_items`

- id
- source
- source_event_id
- reporter
- message
- workspace
- page_url
- status
- classification
- confidence
- risk
- autonomy_level
- canonical_feedback_id
- created_at
- resolved_at

### `feedback_events`

- id
- feedback_id
- event_type
- actor
- model
- summary
- evidence_json
- code_sha
- created_at

### `feedback_artifacts`

- feedback_id
- artifact_type (`screenshot`, `log_excerpt`, `patch`, `test_output`, `resolution_note`)
- uri/reference
- metadata_json

## 22. Dashboard changes

Owner feedback view should show:

- current state;
- GREEN/AMBER/RED badge;
- confidence;
- root-cause summary;
- autonomous actions completed;
- current blocker if any;
- commit/deployment revision;
- production verification;
- reporter reply;
- full audit trail expandable below.

For GREEN resolved items, Gert should not need to open them unless interested.

Recommended default owner filters:

- `Needs my decision`
- `Autopilot working`
- `Resolved automatically`
- `Failed / needs attention`

## 23. Notification policy for Gert

Do not send a notification for every GREEN step.

Notify Gert for:

- AMBER/RED escalation;
- failed deployment or failed production verification;
- security/privacy suspicion;
- repeated autonomous failure;
- daily/periodic digest of GREEN resolutions if desired.

A digest line can be compact:

`3 feedback items resolved automatically today; 1 item needs a product decision.`

## 24. Safe initial rollout

Rollout should be staged.

### Phase 1: shadow mode

Autopilot performs triage, root-cause proposal, patch proposal, test plan, and drafted resolution note, but does not write/deploy.

Purpose: validate classification accuracy against real feedback.

### Phase 2: GREEN code + test, no autonomous deploy

Autopilot may make low-risk changes on `work` and run tests. Gert still approves deploy.

### Phase 3: full GREEN autopilot

After enough clean cases, enable deployment + production verification + reporter response for GREEN items.

AMBER and RED behavior remains unchanged.

## 25. Metrics

Track:

- feedback volume;
- percentage GREEN/AMBER/RED;
- autonomous resolution rate;
- median time to resolution;
- reporter-info requests;
- rollback rate;
- failed-verification rate;
- reopened feedback rate;
- false-GREEN rate;
- number of Gert interventions;
- model/API cost per resolved item.

The critical quality metric is `false-GREEN rate`: cases where the system autonomously declared a fix correct and later had to reopen or rollback it.

## 26. Acceptance criteria for full autonomy

Enable Phase 3 only after a representative shadow/pilot set shows:

- no protected action executed autonomously;
- reliable duplicate detection;
- GREEN classification agrees with manual review at a high rate;
- every autonomous patch has a recorded test result;
- every deployed fix has explicit production verification;
- reporter replies are only sent after verification;
- all actions are reconstructable from the audit log;
- rollback/escalation behavior has been tested.

## 27. Recommended first implementation slice

Build the smallest complete vertical slice:

1. Slack/webhook intake;
2. persistence + idempotency;
3. triage result with confidence/risk/autonomy;
4. orchestrator job creation;
5. GREEN investigation and patch runner against `work`;
6. test gate;
7. stop before deploy initially;
8. owner dashboard displays proposed resolution + audit trail;
9. reporter-response draft generated but not sent.

Once this runs correctly on real feedback, enable autonomous deployment and Slack replies as separate feature flags.

Suggested flags:

```text
FEEDBACK_AUTOPILOT_ENABLED=true
FEEDBACK_AUTOPILOT_WRITE_CODE=true
FEEDBACK_AUTOPILOT_DEPLOY=false
FEEDBACK_AUTOPILOT_REPLY=false
FEEDBACK_AUTOPILOT_MAX_RISK=LOW
```

Deployment and reporter replies should only be switched on after shadow/pilot validation.

## 28. Non-negotiable invariants

1. Never merge to `main` autonomously.
2. Never claim an issue is fixed before production verification.
3. Never make protected external/data actions under GREEN.
4. Never hide failed tests, failed deployments, or uncertainty.
5. Never allow an LLM confidence statement alone to authorize a risky action.
6. Every write/deploy/user reply must be linked to one feedback ID.
7. Every autonomous change must have a rollback reference or a documented reason why rollback is unnecessary/safe.
8. Gert receives decisions, not raw debugging work.
