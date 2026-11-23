# Increment 4 — Requirements and Architecture agents (B14)

## What runs
- `AgentRunner` (`src/aieos/agents/runner.py`) executes the `requirements` and `architecture` steps of the
  delivery workflow with our provider-neutral agent loop (ADR-0002; it replaced the OpenAI Agents SDK). For each run:
  1. Loads the run's published agent version and refuses to run if the deployed contract or prompt differs by
     SHA-256 (`ContractViolation`). The shared untrusted-content preamble is now part of every prompt's hash.
  2. Uses the run's pinned, metered model (workflow and per-run budgets, fallback, circuit breaker).
  3. Builds the agent from its contract: strict output schema, exactly the granted tools (with strict
     argument schemas), all tool results and user input wrapped as untrusted data.
  4. Validates and grounds the output. On a violation it re-runs with the reasons appended, up to the
     contract's `max_retries`, then fails with `AgentOutputRejected`.
  5. Stores the output as an artifact through the gateway (`artifacts.write`).
- Tool calls go through `ToolCallWorkflow` to the gateway worker (see ADR-0001); the agent worker holds no tool
  credentials.

## Grounding (the "never invent requirements" rule, enforced)
- Requirements: every `request` source must be a quote that appears in the request, every `clarification`
  source an id that was answered, and every `repository` source a file read in the same attempt.
- Architecture: tasks may only reference existing acceptance criteria, and every criterion must be covered.
- Blocking open questions pause the workflow (B10); architecture refuses a spec that still has them.

## Evals
- `engineering/evals/requirements/`: 5 golden cases (clear request, vague request, prompt injection inside the
  request, contradictory request, answered clarification). Checks cover grounding, blocking-question
  expectation, required mentions and forbidden terms.
- `aieos-eval` runs them on real models with a hard USD cap; `.github/workflows/evals.yml` runs it on demand.

## Fixed along the way
- Migration 0004: workflow budget totals were `numeric(12,4)` while per-call costs are `numeric(12,6)`, so every
  call rounded the running total (recorded $0.0040 for $0.00396 of calls). Totals now use 6 decimals.
- `ToolCallWorkflow` rejects duplicate ids, so a finished tool call is never scheduled again.

## Verified (2026-11-21)
- 268 passed, 6 skipped. New: 17 agent tests through the real SDK runner with scripted models (tool calls,
  wrapping, grounding retries, invalid JSON, tool and turn limits, injection escape, eval scoring and budget),
  7 end-to-end step tests (activity → runner → metered model → gateway → PostgreSQL + S3), and sandbox
  validation of `ToolCallWorkflow`.
- ruff, mypy --strict (71 files), import-linter, pip-audit clean; Alembic upgrades to 0004.
- **Not run:** the golden set against real models (no keys, model ids or prices configured).
- The 6 Temporal tests, including the `ToolCallWorkflow` bridge, pass in CI (see increment-3.md).

## B14b — provider-neutral model layer (ADR-0002)
- Our own agent loop (`aieos.agents.loop`) replaced the OpenAI Agents SDK. A run ends only through a
  validated `submit_output` tool call; schema mistakes are returned to the model inside the run.
- Plain-HTTP adapters: `anthropic`, `gemini`, `openai`, `openai_compatible` (`aieos.providers.adapters`).
  No vendor SDK is installed. Fallback works across vendors in the middle of a run.
- Provider rejections (bad model id, key, schema) fail the step as `ModelRequestRejected` instead of being
  retried by Temporal.
- Default contracts are 1.1.0: Anthropic for requirements/architecture/developer/supervisor, Gemini for
  QA/Security. Re-run `aieos-admin publish-agents` after deploying.
- Not yet verified against live APIs: adapter tests use mocked HTTP. Run `make eval` with real keys first.
