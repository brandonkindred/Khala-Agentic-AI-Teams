# Collapse duplicate deliver.py wrappers into one shared binder

**Date:** 2026-07-22  
**Status:** Approved for implementation planning

## Goal

Eliminate the byte-identical `_git_ops()` / `run_deliver()` wrappers in
`backend_code_v2_team/phases/deliver.py` and
`frontend_code_v2_team/phases/deliver.py` by folding that wiring into a single
shared factory, while keeping each team module as the monkeypatch boundary for
git helpers.

## Motivation

Both team deliver modules independently define the same `_git_ops()` and
`run_deliver()` that only inject team-local `models`, `DELIVER_COMMIT_MSG_TEMPLATE`,
and a module logger into the already-shared `run_deliver_impl`. The orchestration
logic is not duplicated — only the binder is. Maintaining two copies invites
drift for no behavioral gain.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Shape | `make_run_deliver(...)` factory in `shared/phases/deliver.py` |
| Monkeypatch strategy | Team modules keep importing git helpers into their namespace; binder reads them off `git_ns` at call time |
| Team file shape | Imports + `run_deliver = make_run_deliver(...)` + `__all__` / `DEVELOPMENT_BRANCH` re-export |
| `run_deliver_impl` | Untouched |
| Test / monkeypatch changes | Existing team-module patches stay; add a focused unit test for the binder |
| Scope | Backend + frontend code-v2 deliver wrappers only |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `shared/phases/deliver.py` | Add `make_run_deliver`; leave `run_deliver_impl` body unchanged |
| `backend_code_v2_team/phases/deliver.py` | Collapse to bind site |
| `frontend_code_v2_team/phases/deliver.py` | Collapse to bind site (same shape) |
| `tests/` (SE team) | Add binder unit test; leave existing deliver phase tests intact |

### Data flow

```text
orchestrator
  → team.phases.deliver.run_deliver(...)   # public API unchanged
      → make_run_deliver-bound closure
          → DeliverGitOps from git_ns attrs (call-time lookup)
          → run_deliver_impl(..., ops=..., models=..., commit_msg_template=..., logger=...)
```

### `make_run_deliver` contract

Preconditions:

- `git_ns` exposes callables named `abort_merge`, `checkout_branch`,
  `commit_working_tree`, `create_feature_branch`, `delete_branch`, `merge_branch`,
  and `write_agent_output` (the team module after its git/writer imports).
- `models` satisfies `PhaseModels` (same requirement as `run_deliver_impl`).
- `commit_msg_template` has `{scope}` and `{summary}` slots.
- `logger` is a `logging.Logger`.

Postconditions:

- Returns a callable `run_deliver` with the same keyword-only public signature
  the team modules expose today (`task_id`, `repo_path`, `files`, `summary`,
  `task_title`, `tool_agents`, `task_description`, `feature_branch_name`,
  `merge_to_development`).
- Each invocation builds a fresh `DeliverGitOps` from **current** `git_ns`
  attributes so monkeypatches applied to the team module after bind time still
  take effect.
- Delegates entirely to `run_deliver_impl`; introduces no new control flow.

### Team module shape

Each team `phases/deliver.py`:

1. Imports the git helpers and `write_agent_output` into its own namespace
   (required patch surface).
2. Re-exports `DEVELOPMENT_BRANCH`.
3. Imports team `models` and `DELIVER_COMMIT_MSG_TEMPLATE`.
4. Binds once:

   ```python
   run_deliver = make_run_deliver(
       git_ns=sys.modules[__name__],
       models=_models,
       commit_msg_template=DELIVER_COMMIT_MSG_TEMPLATE,
       logger=logger,
   )
   ```

5. Exposes `__all__ = ["DEVELOPMENT_BRANCH", "run_deliver"]`.

No duplicated `_git_ops` / `run_deliver` body remains in either team file.

## Monkeypatchability

Existing tests in `test_v2_phases.py` patch names on the team deliver module
(e.g. `deliver.create_feature_branch`). That continues to work because:

1. The team module still imports those names into its globals.
2. The binder resolves them via `getattr(git_ns, ...)` (or equivalent attribute
   access) **inside** each `run_deliver` call, not at bind time.

Orchestrators keep `from .phases.deliver import run_deliver` unchanged.

## Testing

- Keep all existing frontend/backend deliver tests in `test_v2_phases.py`
  (behavior regression gate).
- Add a unit test that builds a fake `git_ns`, binds via `make_run_deliver`,
  patches one callable on that ns after bind, stubs `run_deliver_impl`, and
  asserts the `ops` argument passed into the stub holds the patched callable.
- `make test` and `make lint` from `backend/`; ≥90% line coverage on touched
  files.

## Out of scope

- Any change to `run_deliver_impl` itself.
- Review-phase extraction / dedup.
- DevOps or `ai_agent_development_team` deliver modules.
- Retargeting existing tests to patch `shared.git_utils` or the shared binder.

## Complexity

**1** — mechanical dedup with a locked monkeypatch strategy and no behavioral
ambiguity.
