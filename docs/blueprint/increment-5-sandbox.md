# Increment 5 — Local Docker sandbox (first part of B13)

The sandbox is where agent-written code and tests run. The container is the security boundary,
because running a repository's tests executes arbitrary code. The command allowlist only keeps
agents on task.

## Isolation (`src/aieos/sandbox/docker.py`)
| Control | Setting |
| --- | --- |
| Network | `--network none` (loopback only) |
| Filesystem | Read-only root; `/workspace` (exec) and `/tmp` (noexec) are size-capped tmpfs |
| Identity | Runs as `65534:65534` (nobody), `--cap-drop ALL`, `no-new-privileges` |
| Resources | CPU, memory with swap disabled, PID limit; every command under `timeout -s KILL` plus a host deadline |
| Secrets | None: no env values except `HOME`, no host mounts, no credentials |
| Tracking | Labels for org, workflow, agent run and creation time; per-workflow cleanup plus an age-based reaper |
| Optional | `SANDBOX_RUNTIME=runsc` (gVisor) where installed |

The Docker socket is root-equivalent, so only the tool-gateway worker uses it, run on the host for local
development and on CI runners. Never mount the socket into a container. AWS will use one Fargate task per
sandbox instead (B13 continuation).

## Tools (gateway handlers, Level 2)
`sandbox.write_file`, `sandbox.run_command` (allowlisted program, no shell), `sandbox.run_tests` (pytest,
validated selector), `sandbox.export_patch` (stored as a `patch` artifact). One sandbox per agent run,
created on first use. The workflow removes all of its sandboxes on success, failure and cancel (new
`cleanup_sandboxes` activity on the gateway queue).

## Patch integrity
Sandboxed code can reconfigure git. The patch is read with external diff drivers, textconv and the global
attributes file disabled, and with `--text`, so a repository `.gitattributes` such as `*.py -diff` cannot
turn a source change into an opaque "Binary files differ" line. The push path (next) will also reject
patches that touch binary files or `.gitattributes`.

## Verified
- 18 unit tests with a fake Docker CLI: exact isolation flags, path and command validation, call
  sequences, truncation, cleanup and reaping.
- 10 real-Docker tests (`tests/sandbox`, CI job `sandbox`): non-root without capabilities, read-only root,
  no network, memory/time/process/disk limits, edit → test → patch, git-trick resistance, cleanup.

## Next (rest of B13)
GitHub App snapshot of the repository into the sandbox, the validated push path to `aieos/*` branches,
and the Fargate sandbox backend.
