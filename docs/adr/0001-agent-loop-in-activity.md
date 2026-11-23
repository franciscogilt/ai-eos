# ADR-0001: Agent steps run inside one activity (deviation from blueprint R1)

- Status: accepted for B14/B15; revisit in Phase 11
- Date: 2026-11-21

## Context
Blueprint refinement R1 runs each agent's loop inside the Temporal workflow through the OpenAI Agents
SDK plugin: every model call and tool call becomes its own activity. That wiring can only be exercised
against a Temporal server, and the environment that built B14 cannot download one. It would have shipped
untested.

## Decision
One agent step = one `run_agent_step` activity on the agent worker. The agent loop runs there
with the metered model. Tools still never run in the agent worker: each tool call is a small
`ToolCallWorkflow` whose only step is the `execute_tool` activity on the gateway's task queue. The workflow
id derives from the call's idempotency key, and duplicate ids are rejected, so a call runs at most once.

## Consequences
- Credential separation and central authorization are unchanged: the gateway is the only path to tools.
- Durability is per step, not per model call. A worker crash mid-step re-runs the step: tokens are spent
  again, and tool calls get new call ids (new agent run). This is safe for B14 agents, whose tools are
  read-only or write content-addressed artifacts. Before an agent with non-idempotent tools (sandbox
  commands, B15) goes to production, either move to R1 or give those tools step-stable call ids.
- The agent loop, factory, grounding and metering are fully tested without Temporal.
- Revisit: run each model call and tool call of the loop as its own activity (our loop, since ADR-0002
  replaced the Agents SDK) once CI covers it. The `AgentRunner` interface, contracts and gateway do not change.

## Update (2026-11-23): resumable steps
The step still runs in one activity, but it no longer starts over after a crash (migration 0006):
- **One run per step attempt.** There is one agent run per (workflow, step, attempt). A retried activity
  reuses that run, so it keeps its id, its sandbox and its tool-call ids.
- **Checkpoints.** After every model reply (before its tools run) and after every set of tool results, the
  runner saves the attempt, its feedback, the conversation and the tool-call counter to
  `agent_run_checkpoints` (RLS-scoped).
- **Resuming.**
  - A retry continues from that state, so no model call is repeated.
  - A reply whose tools had not run yet runs them, with the same call ids; the gateway returns
    recorded results for calls that already finished.
  - Platform checks (tests, exports, scans) use call ids that are stable for the attempt, so they
    dedupe the same way.
  - A step that had already finished returns its recorded result.
- **Remaining gap.** A model reply that arrived but was not yet saved is requested again, once.

Moving each call into its own activity is no longer needed for correctness. It would add only finer
Temporal visibility.
