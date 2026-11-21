# Increment 3 — Workflow, tool gateway, approvals (B10, B11, B12)

## B10 FeatureDeliveryWorkflow (`src/aieos/orchestration/`)
- `process.py` holds the orchestration logic and has no Temporal dependency. Every side effect goes through a
  `Runtime`: activities, durable waits, cleanup. `workflows.py` is a thin Temporal shell; unit tests drive the
  same logic with an in-memory runtime.
- Order: requirements (up to 3 clarification rounds, waiting on a signal) → architecture → plan (DAG, persisted)
  → per task in dependency order: developer → independent QA/Security review (up to 2 rework rounds, findings
  fed back) → gate → `github.create_pull_request` → `github.merge_pull_request`. Every tool call goes through
  the gateway.
- Approvals: the workflow waits for the `approval_decided` signal and polls the database every 5 minutes as a
  backstop, so a lost signal costs minutes rather than a stuck workflow. A retried tool call reuses its call id;
  the gateway then finds the approval and executes.
- Cancel: `cancel_requested` signal or Temporal cancellation → cleanup activity (closes runs and pending
  approvals; sandboxes join in B13) → status `cancelled`.
- API: create (idempotent per key; started after commit; 503 + `pending` row if Temporal is down, retry with the
  same key), list, get, cancel, answer clarifications.
- Agents are not wired yet: `run_agent_step` uses `NotConfiguredAgentRunner`, which fails fast and is not
  retried. B14 replaces it, using the agent loop inside the workflow via the OpenAI Agents SDK plugin.

## B11 Tool gateway (`src/aieos/gateway/service.py`, task queue `aieos-tool-gateway`)
- One `tool_executions` row per idempotency key (`workflow/agent_run/call_id`), locked `FOR UPDATE`. A finished
  call returns its stored result. The same call id with different arguments is denied.
- Decisions use the agent's *stored* contract version, the project policy, the live budget and matching
  approvals. The decider's role is re-checked at use time.
- `require_approval` creates exactly one pending approval per request (enforced by a partial unique index).
  Level 4 approvals record the workflow creator as requester, so four-eyes applies.
- Handlers run outside the transaction, under a 15-minute lease. Interrupted non-idempotent calls are failed,
  never silently re-run. Transient errors are retried only for idempotent tools.
- Implemented handlers: `artifacts.read`, `artifacts.write`. GitHub and sandbox handlers arrive in B13;
  until then those tools fail with "no handler".

## B12 Approvals
- API: list/get for any member. To decide, the role needs `approval.decide` (approver, owner) and must be in the
  project's `approver_roles`. Checks: optimistic `version`, expiry, four-eyes for Level 4.
- The decision commits first, then the workflow is signalled; `signal_delivered=false` tells the user the poll
  will pick it up. Admins cannot approve by default (separation of duties).
- CLI `aieos`: standard library only; the token is read from the environment, never from a flag.

## Verified (2026-11-21)
- 243 passed, 5 skipped. These include 15 orchestration-logic tests (clarification, rework, approvals, lost
  signal, expiry, cancel, failures), 12 gateway tests against PostgreSQL, and 21 workflow/approval API and CLI tests (20 + the orchestrator-down case).
- The workflow module passes Temporal's sandbox validation (a deliberately non-deterministic workflow is
  rejected by the same check).
- Alembic upgrades to 0003. ruff, mypy --strict (61 files), import-linter and pip-audit are clean.
- Temporal end-to-end tests (`tests/temporal`) could not run in the build session, because its network policy
  blocks the test-server download. Their first CI run (2026-11-21, PR #1) found that the restart test hung:
  the time-skipping server never hands a sticky task queue to a new worker. That test now runs with workflow
  caching disabled, which also replays every step from history. **All 6 pass in CI**: approval signal,
  worker-restart resume (B10 acceptance), lost-signal polling, cancel signal, Temporal cancellation, and the
  tool bridge. CI sets `AIEOS_REQUIRE_TEMPORAL=1`, so they fail rather than skip there.
