# Design: SE base — resolve previous content via git revision

Date: 2026-08-07

## Goal

Provide a fail-open helper that, for each requested path, reads file text from
a **caller-supplied git revision** under `repo_path` (via `git show`) and
returns hits plus explicit misses — without aborting the whole call when git
is unavailable or a single path fails.

## Context

Diff-first SE review needs an `old_contents` map for `change_surface` pair
builders. The on-disk leaf already returns literal workspace bytes (often
post-execution *new* content). This leaf supplies revision-scoped blobs when
callers configure a revision. A later aggregator composes disk + git; this
leaf does not decide priority or call disk.

Existing infrastructure:

- `code_review_agent/previous_content.py` — disk reader +
  `PreviousContentDiskResult`
- `shared.git.git_utils._run_git(..., merge_stderr=False)` — already documented
  for clean `git show <rev>:<path>` stdout (no stderr pollution on success)

## Decisions

| Topic | Choice |
|---|---|
| Revision | Required caller-supplied `revision` string (no default `HEAD` / `HEAD~1` policy) |
| Module | Extend `code_review_agent/previous_content.py` |
| Result type | Rename to neutral `PreviousContentResult`; keep `PreviousContentDiskResult` as alias |
| Git transport | `shared.git.git_utils._run_git` with `merge_stderr=False` |
| Blank inputs | Blank/whitespace `repo_path` or `revision` → `ValueError` |
| Fail-open | Unusable git/repo/revision → all unique paths miss; per-path failures → miss; never abort batch |
| Path identity | Deduplicate by exact path string; result keys are those strings |
| Path safety | Strip leading `/`; blank or `..` / escape-like paths → miss (do not pass unsafe specs to git) |
| Size cap | Inherit `DEFAULT_MAX_FILE_BYTES`; oversize blob → miss |

## Scope

### In scope

- `read_previous_content_from_git(repo_path, revision, paths) -> PreviousContentResult`
- Shared result rename + disk alias
- DbC docstrings
- Unit tests: real mini-repo fixture and/or mocked `_run_git`; hit, miss, unavailable git, blank revision

### Out of scope

- On-disk reader behavior changes (beyond shared type rename/alias)
- Aggregating disk + git into one map
- Change-surface / `CodeReviewInput` / `v2_review` wiring
- Choosing which revision the pipeline should pass

## Public contract

### `PreviousContentResult`

```python
@dataclasses.dataclass(frozen=True)
class PreviousContentResult:
    contents: dict[str, str]   # path -> blob text (hits only)
    misses: frozenset[str]     # paths that were not readable hits
```

`PreviousContentDiskResult = PreviousContentResult`  # backward-compatible alias

Invariants (unchanged from disk leaf):

- Every distinct input path string appears in exactly one of `contents` or `misses`
- `contents.keys()` and `misses` are disjoint
- `len(contents) + len(misses)` equals the number of unique path strings

### `read_previous_content_from_git`

```python
def read_previous_content_from_git(
    repo_path: str,
    revision: str,
    paths: Iterable[str],
) -> PreviousContentResult:
    ...
```

Preconditions:

- `repo_path` is strip-nonempty; otherwise raise `ValueError`
- `revision` is strip-nonempty; otherwise raise `ValueError`
- `paths` is an iterable of strings (may be empty)

Postconditions:

- Returns `PreviousContentResult` as above
- Empty `paths` → empty `contents` and empty `misses`
- Duplicate identical path strings are read once
- Never raises for missing blobs, bad revisions, missing `.git`, timeouts, or
  other git/environment failures once preconditions hold

## Algorithm (sketch)

1. Validate strip-nonempty `repo_path` and `revision` (else `ValueError`).
2. Build unique path list (exact-string dedupe, first-seen order optional).
3. Preflight: if `repo_path` is not a usable git work tree **or**
   `git rev-parse --verify <revision>^{commit}` (or equivalent) fails →
   return all unique paths as misses.
4. For each unique path:
   - Normalize for git: strip; drop leading `/`; if empty or contains `..`
     path segments / would escape → miss
   - `git show <revision>:<normalized>` via `_run_git(..., merge_stderr=False)`
   - Non-zero exit → miss
   - Over `DEFAULT_MAX_FILE_BYTES` → miss
   - Else hit with stdout text

## Error / miss modes

| Situation | Result |
|---|---|
| Blob exists at revision, within size cap | Hit |
| Path not in tree at revision | Miss |
| Blank / whitespace-only path | Miss |
| Unsafe path (`..`, escape) | Miss |
| Oversize blob | Miss |
| Bad / unknown revision (after non-blank check) | All paths miss |
| No `.git` / not a repo / git missing / timeout | All paths miss (or per-path miss if failure is path-scoped) |
| Blank `repo_path` or `revision` | `ValueError` |

## Tests

File: `software_engineering_team/tests/test_previous_content_git.py`

- Hit: init repo, commit `a.py`, `revision=HEAD` (or commit SHA) → contents match
- Miss: path not in revision → in `misses`
- Unavailable: `repo_path` without `.git` → all misses, no raise
- Bad revision: non-blank junk rev → all misses, no raise
- Blank `revision` / blank `repo_path` → `ValueError`
- Fail-open batch: one committed path + one absent → hit and miss
- Alias: `PreviousContentDiskResult is PreviousContentResult` (or isinstance compatibility)

Prefer a small real `git init` fixture for the hit path; mocking `_run_git` is
acceptable for unavailable-git / timeout cases.

## Placement notes for siblings

- Disk and git stay in the same module with the shared result type
- Aggregator composes the two results; this leaf only produces the git slice
