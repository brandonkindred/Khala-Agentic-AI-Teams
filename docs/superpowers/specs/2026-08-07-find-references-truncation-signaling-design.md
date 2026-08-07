# Design: find_references truncation signaling and no-reader behavior

Date: 2026-08-07

## Goal

When a `find_references` scan is truncated or no `repo_reader` is attached,
return explicit messages that do not imply a complete repository search — so
agents never mistake a partial or unavailable search for “no references exist.”

## Context

`find_references` already searches the submission then fills remaining
`max_matches` from `repo_reader` via `_search_repo_references`, returning
`path:line` hits (or `No references for {symbol!r}.`). It does not yet surface
truncation or no-reader caveats.

`side_effect_impact_pass.search_repository` already models the desired UX:
explicit “no repository access” when no reader; truncated empty vs truncated
hits banners when the scan was incomplete.

## Decisions

| Topic | Choice |
|---|---|
| Approach | `_search_repo_references` returns `(hits, truncated)`; format in `find_references` |
| Truncation (repo half) | Same rules as `_search_repository` (file-scan/match caps, unreadable files, Disk listing cap, list_files failure) |
| Truncation (match-cap before repo) | If submission already filled `max_matches`, treat as truncated (repo half never ran) |
| No reader | Always append a submission-only / no-repo-access note (hits or empty) |
| Wording | Mirror `search_repository` vocabulary closely |
| Excerpts / tool wiring / prompts | Out of scope |

## Messaging

| Situation | Output |
|---|---|
| Hits, complete, reader present | `path:line` lines only |
| Hits + truncated | hits + truncated-hits banner (may be more matches) |
| No hits, complete, reader present | `No references for {symbol!r}.` |
| No hits + truncated, reader present | Empty-truncated message (does not prove absence) |
| Any result, no reader | Base hits/empty message **plus** no-repository-access / submission-only note |

Exact strings should follow `search_repository` closely (reuse phrasing where
practical).

## API changes

- `_search_repo_references(...) -> Tuple[List[Tuple[str, int, str]], bool]`
- `find_references` postconditions updated for banners / no-reader note
- Existing exact-string unit tests updated for the no-reader note

## Testing

1. No reader + hits → note present  
2. No reader + empty → note present (not implied whole-repo absence)  
3. Truncated with hits → banner present  
4. Truncated empty → empty-truncated wording  
5. Existing `find_references` tests still pass after string updates  

## Out of scope

- Excerpt attachment
- Pass / `_build_tools` wiring
- Prompt checklist rewrites
- Sharing code with `side_effect_impact_pass._search_repository`
