# Design: SE base — aggregate per-path previous-content hits/misses

Date: 2026-08-07

## Goal

Provide a fail-open aggregator that combines disk and git previous-content
lookups into one stable `contents` map plus `misses` set for change-surface
construction — without aborting the whole call on partial misses.

## Context

Diff-first SE review needs an `old_contents` map for `change_surface` pair
builders. Sibling leaves already exist:

- `read_previous_content_from_disk` — literal workspace bytes (often
  post-execution *new* content; not inherently trustworthy as “previous”)
- `read_previous_content_from_git` — blobs at a caller-supplied revision

This leaf composes them. It does not wire `CodeReviewInput` / `v2_review`.

## Decisions

| Topic | Choice |
|---|---|
| Priority | Git-first: prefer revision blobs; disk fills git misses only |
| Blank / missing `revision` | Disk-only (skip git; do not raise) |
| Shape | Approach 1: pure `merge_previous_content` + thin `resolve_previous_content` orchestrator |
| Module | Extend `code_review_agent/previous_content.py` |
| Result type | Reuse `PreviousContentResult` |
| Blank `repo_path` | `ValueError` (same as leaves) |
| Fail-open | Partial misses never abort once `repo_path` is valid |
| Disk I/O | Only for git misses (or all paths in disk-only mode) — do not re-read git hits from disk |

## Scope

### In scope

- `merge_previous_content(preferred, fallback) -> PreviousContentResult`
- `resolve_previous_content(repo_path, paths, revision=None) -> PreviousContentResult`
- DbC docstrings
- Unit tests: pure merge (overlap / fill / both-miss) and orchestrator mixed hit/miss

### Out of scope

- Changing disk or git leaf behavior
- `CodeReviewInput` / `v2_review` / change-surface wiring
- QA/Security agent inputs
- Choosing which revision the pipeline should pass (caller supplies it)

## Public contract

### `merge_previous_content`

```python
def merge_previous_content(
    preferred: PreviousContentResult,
    fallback: PreviousContentResult,
) -> PreviousContentResult:
    ...
```

Semantics:

- Hits start as `preferred.contents`
- Each path in `fallback.contents` that is not already a preferred hit is added
  from fallback
- Path universe = all keys from both results’ `contents` and both `misses`
- Final `misses` = path universe minus final `contents` keys
- Preferred wins on overlap (fallback text ignored for that path)
- Pure: no I/O, never raises for empty/partial inputs

Invariants (same as `PreviousContentResult`):

- `contents.keys()` and `misses` are disjoint
- Every path in the combined universe appears in exactly one of `contents` or `misses`

### `resolve_previous_content`

```python
def resolve_previous_content(
    repo_path: str,
    paths: Iterable[str],
    revision: str | None = None,
) -> PreviousContentResult:
    ...
```

Preconditions:

- `repo_path` is strip-nonempty; otherwise raise `ValueError`
- `paths` is an iterable of strings (may be empty)
- `revision` may be `None` or blank (disk-only) or strip-nonempty (git-first)

Postconditions:

- Empty `paths` → empty `contents` and empty `misses`
- If `revision` is `None` or blank after strip → return
  `read_previous_content_from_disk(repo_path, paths)`
- Else:
  1. `git = read_previous_content_from_git(repo_path, stripped_revision, paths)`
  2. If `git.misses` is empty → return `git`
  3. `disk = read_previous_content_from_disk(repo_path, git.misses)`
  4. Return `merge_previous_content(git, disk)`
- Never raises for missing blobs, unavailable git, or per-path disk failures
  once `repo_path` is valid (leaves degrade to misses)

## Error / miss modes

| Situation | Result |
|---|---|
| Git hit | Preferred content |
| Git miss + disk hit | Fallback (disk) content |
| Git miss + disk miss | Miss |
| Blank / missing revision | Disk-only result |
| Blank `repo_path` | `ValueError` |
| Partial misses in batch | Remaining paths still returned; no abort |

## Tests

File: `software_engineering_team/tests/test_previous_content_aggregate.py`

Merge (no I/O):

- Preferred wins when both have the same path
- Fallback fills a preferred miss
- Path in both misses → miss
- Empty preferred + fallback hits → fallback contents

Orchestrator:

- Blank / `None` revision → disk-only (tmp_path file hit)
- Git-first mixed: committed file (git hit) + on-disk-only untracked path (git miss, disk hit)
- Both miss → in `misses`, no raise
- Blank `repo_path` → `ValueError`

## Placement notes

- Keep merge + resolve beside the disk/git leaves in `previous_content.py`
- Callers that already hold two `PreviousContentResult` values may use merge alone
- Change-surface continues to consume `contents` as `old_contents`; miss set informs fallback decisions in a later wiring leaf
