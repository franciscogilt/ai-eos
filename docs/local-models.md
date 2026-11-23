# Developing without API bills

AI-EOS can use tools you already have on your machine instead of paid API keys:

| Tool | How AI-EOS calls it | Provider setting |
| --- | --- | --- |
| Claude Code | `claude -p --output-format json --system-prompt … --tools ""` | `kind: cli`, `cli: claude_code` |
| Antigravity CLI (Google AI Pro/Ultra) | `agy --input-format stream-json --output-format stream-json --sandbox --mode plan` | `kind: cli`, `cli: antigravity_cli` |
| Gemini CLI (paid Gemini API key only) | `gemini --output-format json -p …` | `kind: cli`, `cli: gemini_cli` |
| Codex | `codex exec --json --ephemeral --sandbox read-only -` | `kind: cli`, `cli: codex_cli` |
| Ollama | OpenAI-compatible HTTP at `http://localhost:11434/v1`, no key | `kind: openai_compatible` |

## Setup
1. Sign in to each CLI once, interactively (`claude`, `agy`, and `codex` if you use it).
2. In `.env`, set `MODEL_ROUTING_PATH=engineering/model-routing.local.yaml`. For Ollama, also set
   `OLLAMA_BASE_URL=http://localhost:11434/v1`.
3. Run the agent worker on the same machine: `make run-worker-agents`.

`engineering/model-routing.local.yaml` maps the slots the contracts use:
- `anthropic` → Claude Code: requirements, architecture, developer, supervisor;
- `gemini` → Antigravity CLI (`agy`): QA/Security, so review uses a different model family;
- `openai` → Codex: spare.

To use another tool for an agent, edit that slot. For example, point `gemini` at
`cli: codex_cli` or at Ollama.

## How it works
- A CLI returns text, not tool calls. AI-EOS therefore sends the conversation as text, with the tool
  definitions and a strict reply format (one JSON object listing tool calls), and parses the reply
  (`providers/adapters/text_protocol.py`).
- The agent loop, the tool gateway, the sandbox and every check are the same as with the HTTP
  providers. The CLI only produces text; it never acts.
- Each call runs in a new, empty temporary directory, with an environment reduced to what CLIs need to
  find their sign-in. Database URLs, the GitHub App key path and other worker settings are not passed.
- Claude Code's own tools are switched off (`--tools ""`), and Codex runs in its read-only sandbox.
  Antigravity has no flag to switch its tools off. It runs with `--sandbox` in plan mode, where headless
  tool actions are denied by policy, in an empty directory. Do not pre-approve actions for it in
  `~/.gemini/antigravity-cli/settings.json`.
  `extra_args` in the routing file appends flags if your CLI version needs them.
- Prices are 0. Token limits still apply; when a CLI reports no usage, tokens are estimated.

## Limits
- **Verification.** The CLI adapters are tested against stand-ins that follow each CLI's documented
  headless output, not against the real CLIs. Before relying on one, run a single eval on your machine:
  `MODEL_ROUTING_PATH=engineering/model-routing.local.yaml make eval`.
- **Reply format.** Text-based tool calling is less reliable than native tool calls. Expect more
  "please reply with JSON" retries than with the APIs.
- **Gemini CLI.** Since 2026-06-18 Gemini CLI no longer serves free, Google AI Pro or Ultra accounts;
  use Antigravity CLI with those plans.
- **Antigravity CLI.** Some `agy` versions print nothing when run without a terminal (upstream issue
  #76). AI-EOS reports that as a retryable error; update `agy` if you see it. Its stream-json output
  format is only partly documented. The adapter reads the final envelope (`response`, `status`,
  `usage`), so check it with one eval first.
- **Terms.** Each CLI's use is governed by the plan it is signed in with.
