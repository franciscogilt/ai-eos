# ADR-0001: Agent steps run inside one activity (deviation from blueprint R1)

- Status: accepted for B14/B15; revisit in Phase 11
- Date: 2026-11-21

## Context
Blueprint refinement R1 runs each agent's loop inside the Temporal workflow through the OpenAI Agents
SDK plugin: every model call and tool call becomes its own activity. That wiring can only be exercised
against a Temporal server, and the environment that built B14 cannot download one. It would have shipped
untested.

## Decision
One agent step = one `run_agent_step` activity on the agent worker. The Agents SDK `Runner` runs there
with the metered model. Tools still never run in the agent worker: each tool call is a small
`ToolCallWorkflow` whose only step is the `execute_tool` activity on the gateway's task queue. The workflow
id derives from the call's idempotency key, and duplicate ids are rejected, so a call runs at most once.

## Consequences
- Credential separation and central authorization are unchanged: the gateway is the only path to tools.
- Durability is per step, not per model call. A worker crash mid-step re-runs the step: tokens are spent
  again, and tool calls get new call ids (new agent run). This is safe for B14 agents, whose tools are
  read-only or write content-addressed artifacts. Before an agent with non-idempotent tools (sandbox
  commands, B15) goes to production, either move to R1 or give those tools step-stable call ids.
- The SDK runner, factory, grounding and metering are fully tested without Temporal.
- Revisit: move to the plugin (`temporalio-openai-agents`, GA since March 2026) when CI has covered it.
  The `AgentRunner` interface, contracts and gateway do not change.
