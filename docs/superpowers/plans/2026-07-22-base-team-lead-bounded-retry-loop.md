# BaseTeamLead Bounded Retry/Patch-Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded retry/patch-loop extension point to `BaseTeamLead` so subclasses can run debug/patch-style iteration with configurable max attempts, an attempt callable, and a success check — covered by unit tests, with no consumer wiring yet.

**Architecture:** `BaseTeamLead` gains `_run_bounded_retry_loop(*, max_iterations, attempt, is_success)` that loops `attempt(i)` for `i` in `0..max_iterations-1`, aborts on `None`, returns `(True, result)` when `is_success(result)` is true, otherwise `(False, last_result)` after exhaustion. Exceptions from `attempt` / `is_success` propagate. This plan does not migrate devops consumers.

**Tech Stack:** Python 3.10, pytest, Ruff (via `make lint`)

**Spec:** `docs/superpowers/specs/2026-07-22-base-team-lead-bounded-retry-loop-design.md`

## Global Constraints

- Placement: instance method only on `BaseTeamLead` (no module-level helper, no named result dataclass).
- Abort: `attempt` returns `Optional[T]`; `None` → `(False, None)` and stop.
- Exceptions from `attempt` / `is_success` propagate unchanged.
- Return shape: `Tuple[bool, Optional[T]]` — `(succeeded, result)`.
- Attempt signature: `Callable[[int], Optional[T]]` (0-based iteration index).
- Success check: `Callable[[T], bool]` — only called when `attempt` returns non-`None`.
- Precondition: `max_iterations >= 1` (assert).
- No changes to `devops_team/orchestrator.py`, code-v2 orchestrators, or devops subclassing.
- 90% coverage floor on touched files; `make test` and `make lint` must pass from `backend/`.
- Design-by-Contract: document Preconditions/Postconditions on `_run_bounded_retry_loop`.
- Never reference GitHub issue numbers in code, comments, docs, or commit messages.

## File Structure

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/shared/team_lead_base.py` | `_run_bounded_retry_loop` implementation; module/class doc updates; `TypeVar` import |
| `backend/agents/software_engineering_team/tests/test_team_lead_base.py` | Unit tests for success-on-first, success-after-N, exhausted, abort, precondition |

---

### Task 1: Bounded retry/patch-loop helper (TDD)

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_team_lead_base.py`
- Modify: `backend/agents/software_engineering_team/shared/team_lead_base.py`

**Interfaces:**
- Consumes: existing `BaseTeamLead` / `_make_lead()` test helper
- Produces: `BaseTeamLead._run_bounded_retry_loop(*, max_iterations: int, attempt: Callable[[int], Optional[T]], is_success: Callable[[T], bool]) -> Tuple[bool, Optional[T]]`

- [ ] **Step 1: Write the failing tests**

Append to `backend/agents/software_engineering_team/tests/test_team_lead_base.py` (after the existing `_report_status` tests):

```python
def test_bounded_retry_loop_success_on_first_attempt():
    lead = _make_lead()
    calls: list[int] = []

    def attempt(i: int):
        calls.append(i)
        return {"ok": True, "n": i}

    succeeded, result = lead._run_bounded_retry_loop(
        max_iterations=3,
        attempt=attempt,
        is_success=lambda r: r["ok"] is True,
    )
    assert succeeded is True
    assert result == {"ok": True, "n": 0}
    assert calls == [0]


def test_bounded_retry_loop_success_after_n_attempts():
    lead = _make_lead()
    calls: list[int] = []

    def attempt(i: int):
        calls.append(i)
        return {"ok": i >= 2, "n": i}

    succeeded, result = lead._run_bounded_retry_loop(
        max_iterations=5,
        attempt=attempt,
        is_success=lambda r: r["ok"] is True,
    )
    assert succeeded is True
    assert result == {"ok": True, "n": 2}
    assert calls == [0, 1, 2]


def test_bounded_retry_loop_exhausted_retries():
    lead = _make_lead()
    calls: list[int] = []

    def attempt(i: int):
        calls.append(i)
        return {"ok": False, "n": i}

    succeeded, result = lead._run_bounded_retry_loop(
        max_iterations=3,
        attempt=attempt,
        is_success=lambda r: r["ok"] is True,
    )
    assert succeeded is False
    assert result == {"ok": False, "n": 2}
    assert calls == [0, 1, 2]


def test_bounded_retry_loop_abort_on_none():
    lead = _make_lead()
    calls: list[int] = []

    def attempt(i: int):
        calls.append(i)
        if i == 1:
            return None
        return {"ok": False, "n": i}

    succeeded, result = lead._run_bounded_retry_loop(
        max_iterations=5,
        attempt=attempt,
        is_success=lambda r: r["ok"] is True,
    )
    assert succeeded is False
    assert result is None
    assert calls == [0, 1]


def test_bounded_retry_loop_rejects_non_positive_max_iterations():
    lead = _make_lead()
    with pytest.raises(AssertionError):
        lead._run_bounded_retry_loop(
            max_iterations=0,
            attempt=lambda _i: {"ok": True},
            is_success=lambda _r: True,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

From `backend/` (use the repo venv if present, e.g. `.venv/bin/python` or the main checkout's `backend/.venv/bin/python`):

```bash
python -m pytest agents/software_engineering_team/tests/test_team_lead_base.py::test_bounded_retry_loop_success_on_first_attempt agents/software_engineering_team/tests/test_team_lead_base.py::test_bounded_retry_loop_success_after_n_attempts agents/software_engineering_team/tests/test_team_lead_base.py::test_bounded_retry_loop_exhausted_retries agents/software_engineering_team/tests/test_team_lead_base.py::test_bounded_retry_loop_abort_on_none agents/software_engineering_team/tests/test_team_lead_base.py::test_bounded_retry_loop_rejects_non_positive_max_iterations -v
```

Expected: FAIL — `AttributeError: '_run_bounded_retry_loop'` missing on `BaseTeamLead`.

- [ ] **Step 3: Implement the minimal helper**

In `backend/agents/software_engineering_team/shared/team_lead_base.py`:

1. Update the typing import to include `TypeVar`:

```python
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple, TypeVar
```

2. After the existing imports / before or after `logger = ...`, add:

```python
T = TypeVar("T")
```

3. Mention the helper briefly in the module docstring (one sentence: base also provides a bounded retry/patch-loop via `_run_bounded_retry_loop`).

4. Optionally mention it in the class docstring as an available helper (no new instance state — invariants unchanged).

5. Add method after `_report_status` (before `_run_setup_and_delegate`):

```python
def _run_bounded_retry_loop(
    self,
    *,
    max_iterations: int,
    attempt: Callable[[int], Optional[T]],
    is_success: Callable[[T], bool],
) -> Tuple[bool, Optional[T]]:
    """Run ``attempt`` up to ``max_iterations`` times until success or abort.

    Preconditions: ``max_iterations >= 1``; ``attempt`` and ``is_success`` are callable.
    Postconditions:
      - On success: returns ``(True, result)`` where ``is_success(result)`` is True.
      - On abort (``attempt`` returns ``None``): returns ``(False, None)`` and does
        not call further iterations.
      - On exhausted retries: returns ``(False, last_non_none_result)``.
      - Exceptions from ``attempt`` / ``is_success`` propagate unchanged.
    """
    assert max_iterations >= 1, "max_iterations must be >= 1"
    assert callable(attempt), "attempt must be callable"
    assert callable(is_success), "is_success must be callable"

    last: Optional[T] = None
    for i in range(max_iterations):
        result = attempt(i)
        if result is None:
            return False, None
        if is_success(result):
            return True, result
        last = result
    return False, last
```

Do **not** edit `devops_team/orchestrator.py` or any code-v2/coding_team consumer.

- [ ] **Step 4: Run unit tests to verify they pass**

```bash
python -m pytest agents/software_engineering_team/tests/test_team_lead_base.py -v --cov=agents/software_engineering_team/shared/team_lead_base --cov-report=term-missing
```

Expected: all tests PASS; `team_lead_base.py` line coverage ≥ 90%.

- [ ] **Step 5: Lint and broader SE-team sanity check**

From `backend/`:

```bash
make lint
python -m pytest agents/software_engineering_team/tests/test_team_lead_base.py agents/software_engineering_team/tests/test_backend_code_v2_team.py agents/software_engineering_team/tests/test_frontend_code_v2_team.py -q
```

Expected: Ruff clean; existing team-lead subclass tests still pass.

Full suite when ready:

```bash
make test
```

- [ ] **Step 6: Commit**

Stage just the implementation + tests (and this plan / design if not already committed). Force-add under `docs/superpowers/` if gitignored:

```bash
git add \
  backend/agents/software_engineering_team/shared/team_lead_base.py \
  backend/agents/software_engineering_team/tests/test_team_lead_base.py
git add -f \
  docs/superpowers/specs/2026-07-22-base-team-lead-bounded-retry-loop-design.md \
  docs/superpowers/plans/2026-07-22-base-team-lead-bounded-retry-loop.md
git commit -m "$(cat <<'EOF'
Add bounded retry/patch-loop helper to BaseTeamLead.

Gives subclasses a parameterized attempt/success-check loop for
debug/patch-style retries without wiring devops consumers yet.
EOF
)"
```

---

## Self-review

1. **Spec coverage:** API contract, Optional abort, exception propagation, tuple return, iteration index, tests (first / N / exhausted / abort / precondition), no devops edits, lint/test/coverage — all covered by Task 1.
2. **Placeholders:** None.
3. **Type consistency:** `_run_bounded_retry_loop` name and signature match the approved spec (`max_iterations`, `attempt`, `is_success`, `Tuple[bool, Optional[T]]`).
