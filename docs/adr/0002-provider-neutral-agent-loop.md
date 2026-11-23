# ADR-0002: Provider-neutral model layer and our own agent loop

- Status: accepted
- Date: 2026-11-22
- Supersedes: the master prompt's choice of the OpenAI Agents SDK as agent runtime

## Context
The owner requires that AI-EOS is not tied to any model vendor ("LLM agnostic, not tied to OpenAI in any
way"). Until now the agent runtime was the OpenAI Agents SDK (`openai-agents`, which brings the `openai`
client), and every model, fake and error type was an OpenAI type. Other vendors were reachable only through
an OpenAI-compatible endpoint, which loses vendor features (Anthropic prompt caching, Gemini thought
signatures) and puts a translation layer we do not control in the request path.

Options considered:
1. **Own loop + plain-HTTP adapters** (chosen).
2. Pydantic AI: multi-provider and well maintained, but a large dependency whose agent abstraction would sit
   between our gateway, metering and grounding and the model, the same position the Agents SDK held.
3. Keep the Agents SDK with non-OpenAI models through LiteLLM or compatible endpoints: still OpenAI types
   throughout, which is what the owner asked to remove.

## Decision
- `aieos.providers.llm` defines neutral types: `ChatRequest`, `Message` (user, assistant with tool calls,
  tool results), `ToolSpec`, `ChatResponse` with `TokenUsage`, and two errors (`ProviderTransientError`,
  `ProviderRequestError`).
- One adapter per wire protocol in `aieos.providers.adapters`, each about 100–150 lines of plain `httpx`:
  - `anthropic` — Messages API; caches the system prompt and tools;
  - `gemini` — `generateContent`; resends Gemini thought signatures verbatim;
  - `openai` — Chat Completions;
  - `openai_compatible` — the same protocol at any base URL: gateways, vLLM, Ollama.
  No vendor SDK is installed.
- `aieos.agents.loop` is the agent loop, about 120 lines. Every turn must call a tool. The run ends only
  when the model calls `submit_output` with arguments that validate against the step's Pydantic output
  model. An invalid submission goes back to the model as a tool error in the same run. Grounding retries
  still start a fresh conversation.
- Default contracts name no OpenAI model:
  - requirements, architecture, developer and supervisor use the `anthropic` slot;
  - qa_security uses `gemini`, a different family from the Developer, for independent review;
  - contracts are bumped to 1.1.0.
  Which vendor sits behind each slot, and which model id, is configured in `engineering/model-routing.yaml`.

## Consequences
- **No vendor in the dependency tree.** `openai-agents` and `openai` are removed; `httpx` becomes a
  runtime dependency.
- **Fallback works mid-run, across vendors.** History is neutral, so a run can fall back mid-loop from one
  vendor to another. Opaque per-vendor state (`provider_data`) is echoed only to the vendor that produced
  it. When a Gemini request contains calls made by another vendor, the adapter sends Google's documented
  placeholder signature. Google calls that a last resort that may reduce quality.
- **Uniform structured output.** Structured output no longer depends on each vendor's JSON mode, so it
  works the same everywhere.
- **What the tests cover.** Adapter tests pin our translation against each vendor's documented request
  and response shapes, using mocked HTTP. They cannot prove a live API still accepts it. That proof is
  `make eval` (or the manual `agent-evals` workflow) with real keys, run before relying on a provider.
- **Maintenance.** We now own API-version changes for three protocols. Each adapter is small and isolated;
  a breaking vendor change touches one file.
- **ADR-0001 is unchanged in substance.** The loop still runs inside the `run_agent_step` activity. Its
  "revisit" note now means moving the loop's model and tool calls into individual activities ourselves,
  not adopting the OpenAI plugin.

## Addendum (2026-11-22): console CLIs as providers
- The owner develops with the CLIs they are already signed in to, without paying for APIs.
- `kind: cli` drives Claude Code, Gemini CLI or Codex headless, as text models. It uses a JSON reply
  protocol for tool calls (`adapters/text_protocol.py`) and runs each call in an empty temporary
  directory with a reduced environment.
- Ollama uses the existing `openai_compatible` adapter, with the API key now optional.
- The agent loop and its guarantees are unchanged. Details: `docs/local-models.md`.
