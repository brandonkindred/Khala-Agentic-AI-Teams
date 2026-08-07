# Design: SE base — resolve previous content from on-disk workspace files

Date: 2026-08-07

## Goal

Provide a fail-open helper that, for each path in an SE `execution_result.files`
map, reads the **literal current bytes** on disk under `repo_path` and returns
either the file text (hit) or a clear miss — without aborting the whole call
when a single path fails.

## Context

Diff-first SE review needs an `old_contents` map for `change_surface` pair
builders. Later leaves may fill that map from git and aggregate sources. This
leaf is **disk-only**: it does not decide whether disk text is a trustworthy
“previous” revision — after gated execution the workspace often already holds
**new** content, so a hit may equal `execution_result.files[path]`.
Trustworthiness is left to those later leaves.

`change_surface.py` is pure (no I/O) and already consumes in-memory
`old_contents`. `DiskRepoReader.read_file` already implements root confinement,
size caps, UTF-8 read with `errors="replace"`, and fail-open `None` on miss.

## Decisions

| Topic | Choice |
|---|---|
| Semantics | Approach A: literal on-disk text at call time; no compare-to-new filter |
| Module | `code_review_agent/previous_content.py` (keep I/O out of `change_surface.py`) |
| Implementation | Thin batch wrapper over `DiskRepoReader` / `disk_repo_reader_from_root` |
| Return shape | Named result with `contents: dict[str, str]` (hits only) and `misses: frozenset[str]` |
| Path source | Iterate the given path iterable (typically `execution_result.files` keys); do not require file bodies |
| Path identity | Deduplicate by exact path string; result keys are those strings |
| Empty / blank paths | Treated as miss (same as `DiskRepoReader`) |
| Fail-open | Per-path failures never raise; blank `repo_path` raises `ValueError` at the boundary |
| Size/decode limits | Inherit `DiskRepoReader` defaults (`DEFAULT_MAX_FILE_BYTES`, UTF-8 with replace) |

## Scope

### In scope

- `read_previous_content_from_disk(repo_path, paths) -> PreviousContentDiskResult`
- DbC docstring (preconditions / postconditions)
- Unit tests with `tmp_path`: hit, miss, one bad path does not abort siblings

### Out of scope

- Git revision / `git show` resolution
- Aggregating disk + git into a single base map
- Change-surface emission / `unified_diffs_from_pairs` wiring
- `CodeReviewInput` / `v2_review` integration
- Snapshotting disk **before** execution writes (rollback already has its own snapshot)

## Public contract

### `PreviousContentDiskResult`

```python
@dataclasses.dataclass(frozen=True)
class PreviousContentDiskResult:
    contents: dict[str, str]   # path -> on-disk text (hits only)
    misses: frozenset[str]     # paths that were not readable hits
```

Invariants:

- Every distinct input path string appears in exactly one of `contents` or `misses`
- `contents.keys()` and `misses` are disjoint
- `len(contents) + len(misses)` equals the number of unique path strings in the input
  (duplicate identical path strings are read once)

### `read_previous_content_from_disk`

```python
def read_previous_content_from_disk(
    repo_path: str,
    paths: Iterable[str],
) -> PreviousContentDiskResult:
    ...
```

Preconditions:

- `repo_path` is a strip-nonempty path string; otherwise raise `ValueError`
  (caller bug — clearer than relying on `DiskRepoReader`'s assert)
- `paths` is an iterable of strings (may be empty)

Postconditions:

- Returns a `PreviousContentDiskResult` as above
- For each unique path string: if `DiskRepoReader.read_file(path)` returns text →
  hit keyed by that path string; if it returns `None` → miss with that path string
- Never raises for missing files, path escape, directories, oversize files, or
  `OSError` on individual paths
- Empty `paths` → empty `contents` and empty `misses`

## Error / miss modes (per path)

| Situation | Result |
|---|---|
| Regular file under root, within size cap | Hit (UTF-8 with replacement chars) |
| Missing path | Miss |
| Path escapes `repo_path` (`..`, symlink out, absolute elsewhere) | Miss |
| Directory / non-file | Miss |
| Over `DEFAULT_MAX_FILE_BYTES` | Miss |
| `OSError` while reading | Miss |
| Blank / whitespace-only path string | Miss |

## Tests

File: `software_engineering_team/tests/test_previous_content_ondisk.py`

- Hit: write `tmp_path / "a.py"`, assert contents and that path not in misses
- Miss: request a path with no file → path in `misses`, absent from `contents`
- Fail-open batch: one present + one missing → hit and miss both returned; no raise
- Blank `repo_path` → `ValueError`
- Empty `paths` → empty result

## Placement notes for siblings

- Git-based previous-content resolution can live in the same module (e.g.
  `read_previous_content_from_git`) or a peer file; keep disk and git separable
  for tests
- A later aggregator composes disk/git results into the map `change_surface`
  expects; this leaf only produces the disk slice
