# Coding Team Orchestrator Extract — Repo Context & Progress Config

**Status:** Approved 2026-07-14  
**Date:** 2026-07-14  
**Type:** Structural maintainability refactor (behavior-preserving)

## Problem

`software_engineering_team/coding_team/orchestrator.py` still mixes planning/swarm entrypoints with repo-context caching, progress-band math, and concurrency/cap env parsing. Earlier extracts already moved HITL pause cycles, reasoning capture, team routing, worker factory, and swarm mixins into named collaborators. The remaining helpers make the file harder to navigate and to test in isolation.

## Goals

1. Extract repo-context scanning/caching into `coding_team/repo_context.py`.
2. Extract progress/concurrency configuration into `coding_team/progress_config.py`.
3. Leave orchestrator focused on `run_coding_team_orchestrator` and `CodingTeamSwarm` composition.
4. Preserve behavior byte-for-byte for briefing text, cache reuse, progress mapping, and env floors.

## Non-goals

- Merging with `software_engineering_team/shared/repo_context_cache.py` (budgeted v2 briefing; different semantics).
- Renaming public/env APIs or changing defaults.
- Moving `_feature_branch_name`, `_build_review_evidence`, `_DEFAULT_STACK_SPECS`, or further swarm splits.
- Re-exporting moved symbols from `orchestrator.py` for back-compat.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Module split | Two modules (`repo_context`, `progress_config`) | Matches single-responsibility extract pattern already used in this package |
| Re-exports on orchestrator | None | Callers and tests import the owning module directly |
| Progress/config scope | Env parsers + `_NoopBridge` + `_coding_progress` + progress band defaults | Config and progress reporting are one cohesive surface; review evidence / branch naming stay with swarm/git paths |
| Shared cache reuse | Keep coding-team cache separate | Coding-team briefing uses a file ceiling and full-file contents (`"No files found"`); shared cache is char-budgeted for v2 agents |

## Architecture

```
coding_team/
  repo_context.py      # file filters, enumerate/render/join, read, _RepoContextCache
  progress_config.py   # concurrency/cap parsers, progress band, _NoopBridge
  orchestrator.py      # run_coding_team_orchestrator + CodingTeamSwarm (imports both)
  swarm_implementation.py  # late-binds _no_change_revisit_cap from progress_config
  swarm_review.py          # late-binds _review_concurrency from progress_config
```

### `repo_context.py` ownership

Moved as-is (names unchanged):

- `_CONTEXT_EXTRA_EXTENSIONS`, `_CONTEXT_EXTENSIONS`, `_CONTEXT_EXCLUDE_DIRS`, `_CONTEXT_FILE_CEILING`
- `_context_file_filters`
- `_enumerate_context_files`, `_render_context_file`, `_join_context_parts`
- `_read_repo_context`
- `_RepoContextCache`

Module header follows sibling extracts: structural move, no behavior change. Preserve existing DbC docstrings.

### `progress_config.py` ownership

Moved as-is:

- `NO_CHANGE_REVISIT_CAP`, `_no_change_revisit_cap`
- `REVIEW_CONCURRENCY`, `_review_concurrency`
- `IMPLEMENTATION_CONCURRENCY`, `_implementation_concurrency`
- `_NoopBridge`
- `_DEFAULT_PROGRESS_BASE`, `_DEFAULT_PROGRESS_SPAN`, `_coding_progress`

### Stays in `orchestrator.py`

- `CANCEL_KEY`, `MAX_TASK_REVISIONS`
- `_feature_branch_name`, `_build_review_evidence`, `_DEFAULT_STACK_SPECS`
- `run_coding_team_orchestrator`, `CodingTeamSwarm` (and its mixin composition)

## Call sites

1. **`orchestrator.py`** — import symbols from the two new modules; delete moved bodies; use `_RepoContextCache`, `_coding_progress`, progress defaults, `_implementation_concurrency` via those imports.
2. **`swarm_implementation.py`** — late-bound `progress_config` lookup for `_no_change_revisit_cap`; keep `_orch` for `MAX_TASK_REVISIONS`, `ActivityBridge`, `_feature_branch_name`. Update the module docstring accordingly.
3. **`swarm_review.py`** — late-bound `progress_config` lookup for `_review_concurrency`; keep `_orch` for `ActivityBridge`, `_feature_branch_name`, `_build_review_evidence`, `MAX_TASK_REVISIONS`. Update the module docstring accordingly.

Late-bound module attribute lookup remains required so tests that monkeypatch the owning module still observe patches, and so mixin modules do not create circular imports with orchestrator.

## Testing

- Retarget imports/assertions that currently use `orch_mod._read_repo_context`, `orch_mod._RepoContextCache`, `orch_mod._render_context_file`, `orch_mod._enumerate_context_files`, `orch_mod._no_change_revisit_cap`, `orch_mod._review_concurrency`, `orch_mod._implementation_concurrency`, `orch_mod._coding_progress`, and progress-band defaults to the new modules.
- Preferred: split those unit tests into `tests/test_coding_team_repo_context.py` and `tests/test_coding_team_progress_config.py` when moving imports (same assertions; clearer ownership). In-place retarget in `test_coding_team_orchestrator.py` is acceptable if the extract stays smaller.
- Integration paths that exercise `CodingTeamSwarm.run` / `run_coding_team_orchestrator` stay on orchestrator imports.
- Verification command: targeted pytest for coding-team orchestrator + new test modules; suite must pass with no intentional output/behavior diffs.

## Error handling

No new error paths. Existing best-effort walk/read logging, env parse floors, and progress assert contracts move unchanged with their functions.

## Success criteria

- Moved symbols are defined only in the two new modules (absent from `orchestrator` namespace).
- Existing coding-team tests pass after import retargets.
- Briefing identity invariant holds: `_RepoContextCache.read(path) == _read_repo_context(path)` for the same on-disk state.
- Env parsers still floor concurrency/cap to ≥ 1 and honor documented defaults.

## Risk

Low. Pure structural move with dense existing unit coverage. Primary failure mode is a missed late-bound mixin import still reading `_orch._review_concurrency` / `_orch._no_change_revisit_cap` after deletion — caught by concurrency/cap and swarm tests once imports are updated.
