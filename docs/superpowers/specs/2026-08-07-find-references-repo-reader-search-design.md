# Design: find_references repo_reader search with result caps

Date: 2026-08-07

## Goal

When a `repo_reader` is present, extend `CodebaseIndex.find_references` so it
also searches beyond the submission for symbol hits, under explicit per-call
match and file-scan caps, returning capped `path:line` results.

## Context

Submission-only `find_references` already wraps `CodebaseIndex.search` and
formats hits as `path:line` (or `No references for {symbol!r}.` when empty).
Parent work wants the full search half: submission + repo when a reader is
present. Truncation messaging and excerpt enrichment are sibling leaves.

`side_effect_impact_pass._search_repository` already implements capped
repo-wide substring search (match cap, Disk vs non-Disk file-scan defaults,
skip submission paths, fail-safe). This leaf mirrors that behavior in a
**local** helper next to `find_references` rather than coupling the index to
the side-effect pass.

## Decisions

| Topic | Choice |
|---|---|
| API | Extend existing `find_references` (one call) |
| Order | Submission hits first; fill remaining `max_matches` from reader |
| Repo helper | Private local helper in `false_positive_filter.py` (not shared yet) |
| Hit format | Still `path:line` only |
| Match cap | Shared `max_matches` (default `_SEARCH_MATCH_LIMIT`) across both halves |
| File-scan cap | `40` for non-`DiskRepoReader`; `DEFAULT_MAX_LISTED_FILES` for `DiskRepoReader` |
| Skip | Reader paths already keys of `index.files` |
| Truncation string | Out of scope (sibling) — helper may compute a flag but must not surface it |
| No reader | Unchanged submission-only behavior |
| Tool / prompts | Out of scope |

## Behavior

```text
find_references(symbol, max_matches=_SEARCH_MATCH_LIMIT)
  submission = search(symbol, max_matches)
  if repo_reader and len(submission) < max_matches:
      repo = _search_repo_references(
          remaining=max_matches - len(submission),
          max_files_scanned=Disk ? DEFAULT_MAX_LISTED_FILES : 40,
      )
      hits = submission + repo
  else:
      hits = submission
  format path:line or empty message
```

### Preconditions / postconditions (updated)

- Preconditions: `max_matches` > 0 (else `ValueError`, via `search`).
- Postconditions:
  - Hits are newline-joined `path:line` strings, submission first, then repo,
    total ≤ `max_matches`.
  - Empty when both halves yield nothing (including blank symbol).
  - Never raises for missing symbols or reader failures.
  - Does not attach excerpts or truncation banners.

## Placement

- Update `CodebaseIndex.find_references` in
  `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py`
- Add module-private `_search_repo_references` (or method) in the same file
- Constants for file-scan caps live next to `_SEARCH_MATCH_LIMIT` (mirror
  side-effect values; do not import from `side_effect_impact_pass`)

## Testing

Unit tests in `test_false_positive_filter.py` with `_FakeReader`:

1. Repo hits when reader present and submission has no match
2. Merge under shared `max_matches` (submission first)
3. Submission paths skipped in the repo half
4. File-scan cap limits how many non-submission files are considered
5. No-reader regression still returns submission-only hits / empty message

## Out of scope

- Truncation messaging / no-reader tool wording
- Excerpt enrichment
- `_build_tools` / prompt changes
- Refactoring `side_effect_impact_pass._search_repository` to share code
