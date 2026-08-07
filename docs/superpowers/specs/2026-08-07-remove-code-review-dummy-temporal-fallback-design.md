# Design: Remove code_review dummy Temporal disable + convert hermetic run() tests

Date: 2026-08-07

## Goal

Remove the `LLM_PROVIDER=dummy` fallback from
`code_review_temporal_enabled()` so Temporal dispatch is no longer silently
disabled by the no-LLM harness, and convert every former hermetic
`CodeReviewAgent.run()` unit test so the suite stays green in CI without a
live Temporal server.

## Context

Follows the already-landed removal of the `"pytest" in sys.modules` guard.
Together those two guards were the only test-only backdoors that forced
in-process code-review dispatch. Parent work: SE Temporal-mandatory /
remove test-only Temporal-disable guards.

This change bundles two sibling leaves into one PR:

| Leaf | Role |
|---|---|
| Dummy-fallback removal | Delete `_dummy_harness()` from the enablement gate |
| Test conversion | Keep the unit suite hermetic after the gate is unconditional |

`conftest.py` may keep `LLM_PROVIDER=dummy` for LLM client selection; that
env var must no longer affect Temporal enablement.

## Decisions

| Topic | Choice |
|---|---|
| Approach | force_in_process for coordinator-intent tests + existing execute mocks for Temporal-intent tests |
| Autouse conftest Temporal mock | No — too much hidden global magic |
| WorkflowEnvironment for former hermetic tests | No — higher CI cost; mocks preferred |
| `CODE_REVIEW_TEMPORAL_FORCE` | Keep (still valid over address-sentinel disable in tests) |
| Bundle scope | One PR closes both sibling leaves |

## Production change

File: `backend/agents/software_engineering_team/code_review_agent/temporal/config.py`

1. Delete `_dummy_harness()`.
2. Delete `if _dummy_harness(): return False` from `code_review_temporal_enabled()`.
3. Update module docstring, `_force_enabled` docstring, and
   `code_review_temporal_enabled` postconditions so they no longer mention
   dummy as a Temporal disable.

Post-change enablement:

- `True` when `CODE_REVIEW_TEMPORAL_FORCE` is truthy **and** an address
  resolves; or when force is off and an address resolves.
- `False` only when the resolved address is `None` (disable sentinel /
  explicit empty), regardless of `LLM_PROVIDER`.

Also fix the adjacent comment in `code_review_agent/agent.py` that still
lists “dummy / pytest” as disable reasons (one related sentence only).

### Production-path confirmation

Grep of `code_review_agent/` (excluding tests/docs) shows the dummy check
exists only inside `temporal/config.py`. No other production caller branches
on `LLM_PROVIDER=dummy` to choose thread vs Temporal for code review.
`LLM_PROVIDER=dummy` remains a legitimate LLM-client harness selection
elsewhere; it simply must not gate Temporal.

## Tests

### Gate / dispatch (`test_code_review_temporal.py`)

- Delete `test_dummy_harness_disables`.
- Rewrite `test_run_uses_coordinator_when_temporal_disabled` to assert the
  coordinator path via `force_in_process=True` (and/or
  `TEMPORAL_ADDRESS=none`), not via dummy.
- Coordinator-intent `run()` helpers in this file (`rebuilds_reader`,
  `prefers_live_reader`, `passes_none_reader`, etc.): construct with
  `force_in_process=True`.
- Temporal-path tests that already mock
  `execute_code_review_workflow_sync`: keep; drop redundant
  `_code_review_temporal_enabled → True` patches where the new default
  already enables (optional cleanup, only where safe).

### Former hermetic coordinator suites

Add `force_in_process=True` at each `CodeReviewAgent(...)` construction that
intends in-process coordinator behavior in:

- `test_code_review_agent.py`
- `test_code_review_e2e.py`
- `test_code_review_line_threading.py`
- `test_code_review_coordinator.py` (agent.run sites only)
- `test_review_profiles.py`

No new `WorkflowEnvironment` coverage in this PR. Default (non-integration)
CI must not require a live Temporal server. Document in the PR that
integration-suite runtime is unchanged (no new integration tests).

## Docs

- `docs/ENV_VARS.md` — `TEMPORAL_ADDRESS` (code review): remove
  `LLM_PROVIDER=dummy` from the thread-mode fallback list.
- `docs/ENV_VARS.md` — `CODE_REVIEW_TEMPORAL_FORCE`: reword so it is not
  described as overriding a dummy guard (e.g. re-enables despite an
  address disable / for explicit test forcing when needed).

## Out of scope

- SE startup fail-fast and other parent leaves.
- Broader SE thread-mode test sweeps outside code_review_agent run()
  hermeticity.
- Removing `CODE_REVIEW_TEMPORAL_FORCE`.
- Changing `LLM_PROVIDER=dummy` LLM-client harness behavior.

## Verification

- Grep `code_review_agent/` for `_dummy_harness` / dummy Temporal-disable →
  no production hits.
- Grep `code_review_temporal_enabled` body: no `LLM_PROVIDER` / `dummy`
  branch.
- Run enablement-gate tests + affected `test_code_review_*` /
  `test_review_profiles.py` files; all green without a live Temporal
  server.
- PR body notes integration-suite runtime unchanged.
