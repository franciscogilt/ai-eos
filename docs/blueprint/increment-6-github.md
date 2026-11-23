# Increment 6: GitHub snapshots and the validated push path (B13, second part)

The tool-gateway worker is the only process that holds the GitHub App private key. Agents and sandboxes
never see a token. The key is read from `GITHUB_APP_PRIVATE_KEY_PATH`, a mounted file, never an inline
value.

## Tokens (`src/aieos/integrations/github/app.py`)
- The App JWT (RS256) is valid for 10 minutes and backdated 60 s to allow for clock skew.
- Each installation token is scoped to one repository and to the smallest permission set its caller needs:
  - `contents:read` for snapshots and the repo tools;
  - `contents:write` for pushes;
  - `pull_requests:write` for PRs.
- Tokens are cached until 5 minutes before they expire.
- GitHub errors are split into transient and permanent:
  - transient (retried for idempotent tools): 5xx, 429, rate-limited 403, network errors;
  - permanent: everything else, surfaced to the agent or human as the tool's failure reason.

## One base commit per workflow (migration 0005)
`workflows.base_commit_sha` is set on the workflow's first GitHub use. It takes the head of the default
branch at that moment; if two writers race, the first one wins. A trigger then forbids any change.

Every sandbox of the workflow starts from that commit's tarball, and every push is a commit on top of it.
Snapshots are cached in the gateway worker, keyed by repository and commit. Downloads are capped at
200 MB compressed and 1 GB unpacked; the unpacked size is checked while streaming.

## Read-only repository tools (Level 1)
`repo.list_tree`, `repo.read_file` and `repo.search` read the snapshot held in memory. Nothing is extracted
to disk in the gateway. Search is a case-insensitive substring match, not a regex, because the patterns
come from a model and a regex could be made to run for a very long time.

## `github.push_branch` (Level 3)
Pushes exactly the patch artifact that was reviewed:
1. The branch must be `aieos/<workflow id>` or `aieos/<workflow id>/<name>`. A workflow can never write
   to the default branch, a human's branch, or another workflow's branch.
2. The artifact must be a `patch` from the same workflow. `validate_patch`
   (`src/aieos/integrations/github/patch.py`) rejects:
   - binary content;
   - `.gitattributes`, `.gitmodules`, `.git/` and `.github/workflows/`;
   - symlinks, submodules, and any file mode other than 644 or 755;
   - unusual or unnormalised paths, copies;
   - patches over 200 files or 5 MB.
3. The patch is applied in a fresh sandbox (no network) seeded with the base snapshot. Before applying,
   every touched file is hashed with `git hash-object --no-filters` and compared with GitHub's base tree.
   This catches a snapshot that differs from the repository, for example through `export-subst`.
   Files the patch adds must not already exist.
4. After applying, `git status` must list exactly the files the validator found. Otherwise nothing is
   pushed.
5. The result goes through the Git Data API:
   - blobs, then a tree on the base tree, then one commit whose parent is the base commit;
   - the branch is created, or force-moved (it is in the `aieos/` namespace).
   Re-running gives the same tree. The App has no Workflows permission, so GitHub would also refuse
   workflow changes.

## `github.create_pull_request` (Level 3)
- Opens a PR from an `aieos/<workflow id>` branch to the repository's default branch.
- If the PR already exists, it is returned instead of failing.

## Tests
| Where | What | Runs |
| --- | --- | --- |
| `tests/unit/test_github.py` | Patch rules, branch rules, JWT, token scoping and refresh, error classes, redirect without the token, repo tools, checks before any write | Locally and CI |
| `tests/sandbox/test_github_push.py` | Full push with real Docker and an in-memory GitHub (`tests/github_fake.py`): one commit on the base, deletes and mode changes, the default branch untouched, the push sandbox removed, stale or clashing patches and tampered snapshots refused | CI |
| `tests/sandbox/test_github_live.py` | Snapshot, patch, push and verify against `franciscogilt/aieos-playground` with the test App; the branch is deleted afterwards | CI, same-repository runs |
| `tests/integration/test_github_workflow_repo.py` | Base commit pinned once, the trigger, tenant isolation | CI |

## Not in this increment
- **Several tasks on one branch:** done in Increment 7. Sandboxes stack approved patches, and the push applies them in order.
- **`github.merge_pull_request` and `github.comment`** still have no handler; they fail with "no handler".
- **Snapshots in S3.** The worker downloads them again after a restart.
