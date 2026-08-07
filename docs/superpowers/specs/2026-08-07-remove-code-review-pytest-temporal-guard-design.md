# Design: Remove code_review_agent pytest Temporal-disable guard

Date: 2026-08-07

## Goal

Delete the `"pytest" in sys.modules` fallback in
`code_review_agent/temporal/config.py::code_review_temporal_enabled()` so that
importing pytest no longer forces in-process (thread-mode) code-review
dispatch. After this change, `code_review_temporal_enabled()` never inspects
`sys.modules`.

## Context

Part of the SE Temporal-mandatory work: remove test-only Temporal-disable
guards from the code review agent so dispatch is not silently forced off in
CI or under a pytest import.

Sibling leaves in the same parent:

| Leaf | Role |
|---|---|
| This change | Remove pytest-detection guard |
| Dummy-fallback removal | Remove `LLM_PROVIDER=dummy` Temporal-disable branch |
| Test conversion | Convert hermetic unit tests to mocked/real Temporal |

`conftest.py` still sets `LLM_PROVIDER=dummy` by default, so the remaining
dummy branch keeps the suite hermetic until those sibling leaves land.

## Decisions

| Topic | Choice |
|---|---|
| Approach | Minimal surgical: production gate + one contract test + docs that claim the pytest disable |
| Test for old guard | Rewrite: assert enabled is `True` when env is cleared under pytest |
| Dummy fallback | Leave untouched (sibling leaf) |
| Broader `CodeReviewAgent.run()` test conversion | Out of scope (sibling leaf) |
| Drive-by comment edits in `worker.py` / `agent.py` | Skip |

## Production change

File: `backend/agents/software_engineering_team/code_review_agent/temporal/config.py`

1. Delete the branch:
   ```python
   if "pytest" in sys.modules:
       return False
   ```
2. Remove the now-unused `import sys`.
3. Update module docstring, `code_review_temporal_enabled` postconditions, and
   `_force_enabled` docstring so they no longer say pytest disables Temporal.
   Keep `_dummy_harness()` and `CODE_REVIEW_TEMPORAL_FORCE`.

Post-change enablement (force off):

- `False` when `_dummy_harness()` is true, or when
  `resolve_code_review_temporal_address()` is `None`.
- `True` otherwise (including under pytest with a resolved address).

## Tests

File: `backend/agents/software_engineering_team/tests/test_code_review_temporal.py`

Rewrite `test_enabled_is_false_under_pytest_by_default`:

- Clear `TEMPORAL_ADDRESS`, `LLM_PROVIDER`, and `CODE_REVIEW_TEMPORAL_FORCE`.
- Assert `code_review_temporal_enabled()` is `True` (default address
  `temporal:7233` applies; pytest alone does not disable).
- Rename to reflect the new contract (e.g.
  `test_enabled_is_true_under_pytest_when_env_cleared`).

Leave unchanged: `test_force_flag_*`, `test_dummy_harness_disables`, and
coordinator-path tests that still rely on the dummy guard / explicit mocks.

## Docs

Update only lines that claim pytest disables Temporal:

- `docs/ENV_VARS.md` — `TEMPORAL_ADDRESS` (code review agent default): drop
  “or running under `pytest`”.
- `docs/ENV_VARS.md` — `CODE_REVIEW_TEMPORAL_FORCE`: reword to override the
  `dummy` guard only (pytest no longer listed).

## Out of scope

- Removing the `LLM_PROVIDER=dummy` branch.
- Converting the broader code_review unit suite to mocked/real Temporal.
- SE startup fail-fast behavior (other leaves under the same parent).
- Rewording incidental pytest mentions in `worker.py` / `agent.py`.

## Verification

- Grep `code_review_agent` for `"pytest" in sys.modules` → no hits.
- Grep `code_review_temporal_enabled` body for `sys.modules` → no hits.
- Run the enablement-gate tests in `test_code_review_temporal.py` (resolver +
  enablement section) and confirm green.
