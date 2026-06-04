# Git Commit Identity & Coding-Team Restart Recovery / Continuation

- **Status:** Revised after design review (continuation added); implementation pending
- **Date:** 2026-06-04
- **Owner:** Brandon Kindred
- **Components:** `software_engineering_team/shared/git_utils.py`, `coding_team/api/main.py`, CLAUDE.md (env reference)

## 1. Problem Statement

Three linked failures break the coding team's GitHub-issue flow on the shared
`agents_data` checkout:

**1a. Commits fail because git has no author identity.** Inside the agent
containers neither `user.name`/`user.email` nor the `GIT_AUTHOR_*`/
`GIT_COMMITTER_*` environment variables are set, so every `git commit`
outside a freshly-initialized repo fails:

```
[WARNING] software_engineering_team.shared.git_utils: Could not commit before feature branch:
Author identity unknown
fatal: unable to auto-detect email address (got 'appuser@eb0181682851.(none)')
```

`initialize_new_repo()` sets a hardcoded repo-local identity, but checkouts
that enter the system via the unified API's GitHub clone never get one.

**1b. An interrupted run wedges every later run on the checkout.** An
interrupted job leaves the working tree dirty (the observed failure died
between `git add` and `git commit` — the salvage commit itself failed due to
1a). Any later "Confirm & Start" on that checkout — a retry of the same
issue, **or a brand-new job for any issue after the old job was stopped or
deleted** — hits the branch-prep guard and fails:

```
branch prep failed: working tree has uncommitted changes; clean it before retrying:
A specs/SPEC-007/decisions/007-open-questions.md
```

The guard exists for a good reason — `git checkout -B` would otherwise carry
unrelated dirty files across the branch switch and leak them into the issue's
PR — but it gives the operator no recovery path short of shelling into the
volume.

**1c. Committed progress from an interrupted job is silently orphaned.**
Task work merges into the shared `development` branch during a run;
`khala/issue-N` only catches up via the final fast-forward. Branch prep
resets both (`checkout -B`), so a new job for the same issue discards
everything the interrupted job committed — there is no "pick up where the
previous job left off", even though the work exists in the object store.

## 2. Goals

1. Every git commit/merge performed by platform code has a valid, configurable
   author identity, with sensible defaults and no per-checkout setup.
2. Starting a job for a GitHub issue always recovers a wedged checkout:
   uncommitted changes are committed or preserved out of the way, branch prep
   proceeds on a clean tree, and the job runs.
3. A new job for issue *N* **continues from issue N's prior progress** —
   uncommitted same-issue WIP, local committed work, or a previously-pushed
   `origin/khala/issue-N` — even when the prior job's record was deleted.
4. No agent or recovery path ever destroys work irrecoverably; work belonging
   to a *different* issue is preserved out of the way, never carried into
   issue N's PR.
5. The operator can see what recovery did (log + GitHub issue comments).

## 3. Non-Goals

- **Resuming orchestrator/task-graph state.** The new job regenerates its
  plan. Continuation is at the *repository* level: agents see prior code,
  commits, and history through repo context and their git tools, and build on
  it. Per-task checkpointing is a separate capability.
- **Pruning old rescue branches.** Rescue refs are local-only and cheap;
  cleanup is left to the operator.
- **An LLM-driven commit-vs-discard decision.** Considered and rejected
  (§8) in favor of a deterministic, always-preserve policy.
- **Cross-issue concurrency on one checkout.** Two simultaneous jobs on the
  same owner/repo checkout already contend on the shared `development`
  branch today; this design neither fixes nor worsens that (§6.7).

## 4. Design Overview

Part 1 makes identity ambient: a process-environment shim applied at the
single subprocess choke point all platform git commands flow through. Part 2
builds restart **recovery and continuation** on top of it, driven by one new
piece of durable state — a repo-local marker recording which issue the
checkout is currently working — plus deterministic git-graph checks.

Part 1 is a hard prerequisite of Part 2 — recovery *commits* need an author.
It also independently fixes the in-place salvage commit that
`create_feature_branch` already attempts today.

Why a repo-resident marker: the job store cannot attribute leftovers (jobs
can be deleted — the trigger for this revision), and the git graph alone
cannot say which issue dirty files or `development`-ahead commits belong to.
A `git config` entry in the checkout survives restarts and job deletion, is
never part of any commit or PR, and is written/cleared at well-defined
lifecycle points.

## 5. Part 1 — Configurable Git Commit Identity

### 5.1 Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GIT_COMMIT_USER_NAME` | `Khala` | Author/committer name for all platform git commits |
| `GIT_COMMIT_USER_EMAIL` | `brandon.kindred@gmail.com` | Author/committer email for all platform git commits |

Empty or whitespace-only values fall back to the defaults (matching the
repo's garbage-values-fall-back convention for env knobs). Values are read at
call time, not import time, so tests and runtime reconfiguration behave.

### 5.2 Mechanism

New function in `software_engineering_team/shared/git_utils.py`:

```
def git_identity_env() -> dict[str, str]
```

- Returns a copy of `os.environ` with `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`,
  `GIT_COMMITTER_NAME`, and `GIT_COMMITTER_EMAIL` filled via `setdefault`
  from the configuration above.
- `setdefault` semantics: if the operator already exports any of git's native
  identity variables, those win — the shim only fills gaps. This preserves
  native git behavior for operators who configure git directly.

`_run_git()` passes `env=git_identity_env()` to `subprocess.run` on every
invocation. Because `_run_git` is the single choke point for all of
`git_utils` — and the coding team's agent git tools (`agent_git_tools/
executor.py`) dispatch to `git_utils` functions — every platform commit and
merge gains identity transiently. Nothing is written to any checkout's
`.git/config` for identity, so no checkout-preparation path can "miss" the
setup (the failure mode that caused 1a).

`initialize_new_repo()` keeps writing a repo-local identity (those repos may
later be used by tooling outside `git_utils`) but sources the values from the
same configuration instead of its current hardcoded `"Khala Agent"` /
`agent@khala.local`.

### 5.3 Contracts

`git_identity_env()`
- **Preconditions:** none.
- **Postconditions:** returned dict contains all parent environment entries;
  the four git identity variables are present and non-empty; pre-existing
  values of those variables are unchanged.

`_run_git()` (changed)
- **Postconditions (added):** the spawned git process observes a complete
  author/committer identity.

## 6. Part 2 — Recovery & Continuation in Branch Prep

All changes live in `_prepare_issue_branch()` (`coding_team/api/main.py`) and
its caller `_run_with_github_hooks()`.

### 6.1 The active-issue marker

- Repo-local git config key: `khala.active-issue`, value = the issue number.
- **Written** by `_prepare_issue_branch` once the tree is clean and branches
  are seeded, immediately before returning success.
- **Cleared** by `_run_with_github_hooks` on every terminal path (success,
  recorded failure, orchestrator exception) via `try`/`finally`.
- A marker found at prep time therefore means: *the previous job on this
  checkout terminated abnormally (restart, kill, delete) while working issue
  M*. Leftover dirty files and `development`-ahead commits are attributed to
  issue M.
- Absent marker + leftovers means attribution is unknown → treat as foreign
  (preserve, don't continue from).

### 6.2 Recovery flow (dirty tree)

On entry, read marker `M` (may be unset); the job is for issue `N`.

1. `git status --porcelain` **errored** → fail closed exactly as today
   (state unknowable; no rescue attempted).
2. Tree dirty and `M == N` (same issue): commit in place on the current
   HEAD branch — `wip: recover uncommitted changes from interrupted run` via
   the identity-aware `git_utils.commit_working_tree()`. HEAD's tip becomes
   the strongest continuation candidate (§6.3): mid-run HEAD (`development`
   or a task feature branch) is exactly where the interrupted job stopped.
3. Tree dirty and `M != N` (different issue) or `M` unset: move the dirty
   state out of the way — `git checkout -b <rescue>` (a branch switch carries
   staged, unstaged, and untracked files), commit there, where `<rescue>` is
   `khala/rescue/issue-M-<UTC yyyymmdd-HHMMSS>` when `M` is known, else
   `khala/rescue/<UTC yyyymmdd-HHMMSS>` (e.g. `khala/rescue/20260604-131706`).
   Same-second collisions append `-1` through `-9`; if all ten candidates
   exist, fail closed.
4. Any rescue/recovery step fails → fail closed: today's message plus the
   step's error. Prep never proceeds with a dirty tree and never runs
   `reset --hard`/`clean`.

### 6.3 Continuation flow (seeding the branches)

After the tree is clean:

1. Fetch `origin/<base>` **and** `origin/khala/issue-N` (tolerating absence
   of the latter).
2. Choose the **seed tip** — the first candidate that exists and has commits
   ahead of `origin/<base>` (checked with the existing
   `branch_has_commits_ahead_of` logic):
   1. the interrupted run's tip, when `M == N`: HEAD after the §6.2 in-place
      commit if the tree was dirty, else the local `development` tip;
   2. local `khala/issue-N`;
   3. `origin/khala/issue-N`;
   4. the newest `khala/rescue/issue-N-*` ref (highest embedded
      `yyyymmdd-HHMMSS` timestamp; lexicographic order suffices);
   5. none → fresh start from `origin/<base>` (today's behavior).
3. **Orphan-prevention invariant:** before any `checkout -B`, every local
   branch about to be reset (`development`, `khala/issue-N`) whose tip is
   ahead of `origin/<base>` and not reachable from the chosen seed gets a
   rescue ref first (plain `git branch`, no checkout): named
   `khala/rescue/issue-M-<ts>` when the marker attributes the work to issue
   `M`, untagged `khala/rescue/<ts>` otherwise. No reset performed by prep
   may make commits unreachable.
4. Seed: `checkout -B development <seed>` → `checkout -B khala/issue-N
   development`. Write the marker (`khala.active-issue = N`). Return success.
5. The final `_fast_forward(khala/issue-N, development)` and
   `--force-with-lease` push in `_run_with_github_hooks` are unchanged and
   remain correct: `development` only grows from the seed, and the fetch in
   step 1 refreshed the lease.

### 6.4 Operator transparency

Best-effort GitHub issue comments (failure to comment never fails the job):
- recovery: "♻️ Recovered uncommitted changes from an interrupted run
  (committed on `<branch>`)" or "… preserved on local branch
  `khala/rescue/…`";
- continuation: "▶️ Continuing issue from previous progress: `<seed>`
  (<k> commits ahead of `<base>`)".

Plus warning-level logs for each action.

### 6.5 Why these mechanics

- **In-place WIP commits on `khala/issue-N` are not durable.** A later rerun
  executes `checkout -B`, which resets the branch pointer; only §6.3's
  invariant or a rescue ref keeps such commits reachable.
- **Stashes rot invisibly** on a shared volume; named refs are discoverable
  with `git branch --list 'khala/rescue/*'`.
- **`reset --hard`/`clean -fd` destroys work**, and foreign-vs-own
  attribution comes only from the marker; when it's absent the safe reading
  is "could be operator work" → preserve.
- **The job store can't attribute leftovers** — jobs are deletable by design
  (the scenario motivating this revision). The repo itself is the only state
  that reliably survives.

### 6.6 Contracts

`_prepare_issue_branch()` (changed)
- **Preconditions:** unchanged (`repo_path` is a git checkout; refs are safe).
- **Postconditions:** on success the integration branch `khala/issue-N` is
  checked out, seeded per §6.3, the working tree is clean, and
  `khala.active-issue = N`. Every commit reachable from any local branch on
  entry is still reachable from some local or remote ref. The returned
  success carries recovery/continuation details for caller-side reporting.
  On any failure, no uncommitted work has been deleted and no commit that
  was reachable on entry has become unreachable (a mid-sequence failure may
  leave `development` re-seeded, but §6.3's orphan-prevention invariant has
  already preserved any tip it replaced).

`_run_with_github_hooks()` (changed)
- **Postconditions (added):** `khala.active-issue` is unset on every terminal
  path reached after a successful prep.

### 6.7 Known limitation

Two concurrent jobs on the *same owner/repo checkout* still contend on the
shared `development` branch and on the marker (last prep wins). This is a
pre-existing property of the shared-checkout model, unchanged here; the
duplicate-run guard already serializes per-issue runs.

## 7. Testing Plan

TDD throughout; git behavior tested against real temporary repositories, not
mocks. Global/system git config neutralized per test (`HOME` → empty tmpdir,
`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`) so the suite
reproduces the container's identity-free environment regardless of the
developer's machine.

Part 1 (`software_engineering_team/tests/`):
- Defaults: identity env carries `Khala` / `brandon.kindred@gmail.com`.
- Overrides: `GIT_COMMIT_USER_NAME`/`EMAIL` respected; empty values fall back.
- Precedence: pre-set `GIT_AUTHOR_NAME` etc. are not clobbered.
- End-to-end: `commit_working_tree()` in an identity-free environment
  succeeds with author `Khala <brandon.kindred@gmail.com>` (RED reproduces
  "Author identity unknown" first).
- `initialize_new_repo()` writes the configured identity.

Part 2 (`coding_team/tests/`), each against a real repo with a simulated
"remote" (second local repo or `file://` remote):
- **Recovery:** dirty tree + marker == N → committed in place, prep succeeds,
  new job's branches contain the WIP commit. Dirty + marker != N → rescued to
  `khala/rescue/issue-M-*`, files absent from issue N's branches. Dirty + no
  marker → rescued to untagged ref. `git status` failure → fail closed.
  Rescue-commit failure → fail closed, dirty files still present.
  Name collision → suffixed branch.
- **Continuation:** prior local `khala/issue-N` ahead → seed from it; prior
  `origin/khala/issue-N` only → fetched and seeded; marker == N with
  `development` ahead → seeded from interrupted tip; rescue-ref-only
  progress → seeded from newest `khala/rescue/issue-N-*`; nothing ahead →
  fresh from base (baseline behavior preserved).
- **Orphan-prevention:** `development` ahead with marker != N → rescue ref
  created before reset; every pre-existing commit still reachable afterward
  (checked via `git rev-list --all`).
- **Marker lifecycle:** set on prep success; cleared on orchestrator success,
  recorded failure, and orchestrator exception.
- **Caller surfacing:** recovery/continuation details reach the issue
  comment; comment failure does not fail the job.

Coverage: ≥90 % line coverage on new/changed code per repo policy.

## 8. Alternatives Considered

| Alternative | Rejected because |
|---|---|
| LLM decides commit vs discard | Adds latency and nondeterminism to a *recovery* path; needs a deterministic fallback anyway when the LLM call fails; a wrong "discard" is unrecoverable. Deterministic always-preserve dominates: strictly safer, and the preserved ref lets a human (or future agent) make the keep/drop call later. |
| Job-store lookup for attribution | Jobs can be stopped *and deleted* — the motivating scenario. The checkout must carry its own state. |
| Marker as a tracked file (e.g. `.khala/active-issue`) | Tracked files leak into commits and PRs; `git config` is repo-resident but never part of the tree. |
| `git stash push -u` then proceed | Recoverable but invisible; stash stack on a shared volume rots and is easy to clobber. Named refs are discoverable and auditable. |
| Repo-local `git config user.*` at every checkout-preparation site | Multi-site and ordering-dependent; any new entry path (the GitHub clone was exactly such a miss) reproduces the bug class. |
| `git config --global` in container entrypoints | Spreads configuration across Docker images, untestable in unit tests, doesn't help local (non-Docker) runs. |
| Always reset to `origin/<base>` (status quo) and only rescue | Preserves work but ignores it — fails the "pick up where the previous job left off" requirement. |

## 9. Rollout & Operations

- No docker-compose changes required: code defaults provide the requested
  identity (`Khala` / `brandon.kindred@gmail.com`); the env vars are the
  override knob and get two rows in CLAUDE.md's environment table.
- Affected services pick the change up on the next image rebuild
  (`docker compose … up -d --build`).
- The currently-wedged checkout self-heals on the first run for any issue:
  with no marker present, the dirty `specs/SPEC-007/...` file is preserved on
  an untagged `khala/rescue/*` branch and the job proceeds. (Checkouts wedged
  *before* this feature ships predate the marker, so same-issue dirt from
  them is preserved rather than auto-continued — a one-time cost.)
- Operators can list preserved work with
  `git branch --list 'khala/rescue/*'` and inspect the marker with
  `git config khala.active-issue` inside the workspace.
