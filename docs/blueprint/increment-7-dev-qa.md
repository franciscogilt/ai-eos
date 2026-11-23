# Increment 7: Developer and QA/Security agents (B15)

With this increment the whole delivery loop runs: requirements, then architecture, then for each task
develop and independently review (with rework), then push, then open a PR. Merging still needs a
`github.merge_pull_request` handler (next).

## Principle: platform evidence, not model claims
A model's statements about tests, patches or scans never gate delivery. Those facts come from tool
calls that the platform makes through the gateway, under the step's agent run. They are therefore
authorized, recorded and audited like any other call.

### Developer (`agents/delivery.py`, contract 1.2.0, prompt v2)
1. `sandbox.prepare` (platform-only, never shown to a model) builds the sandbox. It starts from the base
   snapshot, commits the patches already approved in this workflow, and on rework applies the previous
   attempt (uncommitted).
2. The model works and submits a `DeveloperSubmission` (status, summary, tests added).
3. The platform then runs the full test suite and exports the patch itself. The answer is sent back with
   the reason (one fresh conversation in the same sandbox, at most two retries) if:
   - tests fail;
   - the patch is empty;
   - no test file changed;
   - the patch breaks push policy (binary files, `.gitattributes`, workflows and the other push rules).
4. The step's `DeveloperResult` holds the platform's patch artifact and stored test report.

### QA/Security (contract 1.2.0, prompt v2)
1. The platform prepares a fresh sandbox: approved patches committed, the patch under review applied
   uncommitted.
2. Before the model starts, the platform reads the patch, runs the full test suite and runs the security
   scan. The model receives all of these as evidence. The developer's summary is wrapped as untrusted
   data.
3. An answer is sent back if it approves despite failing platform tests or a secret finding, or if
   its criteria results do not cover exactly the task's acceptance criteria.
4. The platform's test report and security report are attached to the `ReviewReport`.

### Security scan (`sandbox.run_security_scan`)
- Bandit (SAST, added to the sandbox image) runs on the changed Python files.
- A secret scan checks the lines the patch adds: private keys, AWS, GitHub, Slack, Anthropic, OpenAI and
  Google keys. The matched value is never returned.
- The dependency audit needs network access, so it runs in CI, not in the sandbox. The result says so.
- The report is stored as a `security_report` artifact.

## Stacking tasks
- The orchestrator keeps the approved patches in task order.
- Each developer and review sandbox starts from base plus those patches, so each patch holds only its
  own task's change.
- After the last approval, `github.push_branch` receives `patch_artifact_ids` in order. It checks each
  patch, combines them into the net change (`net_changes`), applies them in sequence in the push sandbox,
  and makes one commit on the base. Patches pushed out of order do not apply and are refused.
- A rejected patch is never part of the base; the next attempt continues from it as uncommitted work.

## Other changes
- `ToolSpec.model_visible: false` lets the platform call a tool that models never see.
- `export_patch` reports `policy_violations` at once, so the developer can fix them before review.
- The developer contract now retries on `schema_violation` (up to 2): a failed platform check is the
  usual reason.

## Tests
- `tests/unit/test_delivery.py`: platform checks against a scripted gateway. It covers:
  - call ids that are unique per attempt;
  - evidence that never comes from the model;
  - blocked tasks;
  - the QA approval rules;
  - untrusted wrapping;
  - the platform-only tool being hidden from the model.
- `tests/unit/test_process.py`: stacking inputs, rework from the rejected patch, push before the PR.
- `tests/sandbox/test_github_push.py` (real Docker, CI):
  - two tasks stacked and pushed as one commit;
  - out-of-order patches refused;
  - the review sandbox state;
  - bandit and the secret scan;
  - `prepare` refusing foreign or broken patches.

## Merge and comment (B15b)
`github.merge_pull_request` is Level 3, so a human approves it (unless the project auto-approves it).
- The orchestrator passes the PR's `head_sha` with the request. The approval is bound to the request
  digest, so it covers exactly one commit.
- The handler merges only if all of these hold:
  - the PR comes from this workflow's `aieos/<workflow id>` branch in the same repository;
  - it targets the default branch and is open;
  - its head is the approved `head_sha`;
  - that sha is the commit the platform last pushed (from the gateway's own record).
- Anyone pushing to the branch after review therefore blocks the merge. GitHub enforces the same rule
  through the `sha` precondition, and branch protection and required checks still apply. The App cannot
  bypass them.
- It squash-merges, then deletes the `aieos/` branch. Re-running after a crash returns "already merged".
- `github.comment` posts only on this workflow's own PRs.
- The supervisor contract (1.2.0) is now granted `github.push_branch`, `github.comment` and
  `github.merge_pull_request`. The orchestrator acts as the supervisor run; before this, a real push would
  have been denied. No other agent holds these tools (integration test).

## Not yet
- Running agent model/tool calls as separate Temporal activities (ADR-0001). A crash mid-step re-runs the
  step in a new sandbox.
- A live merge test. CI merges only into the in-memory GitHub, so that the playground's default branch
  is not changed on every run.
