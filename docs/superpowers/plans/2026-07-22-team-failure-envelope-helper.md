# Shared Team Failure Envelope Helper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add module-level `build_team_failure_result` and `apply_team_failure` helpers in `team_lead_base.py` that construct/mutate the shared `success=False` + `failure_reason` + optional partial-state envelope, covered by unit tests, with no consumer wiring yet.

**Architecture:** Two pure helpers sit alongside `copy_development_result_fields`. The factory constructs a new result via `result_cls(success=False, failure_reason=..., **partial_state)`. The mutator sets those fields on an existing object and returns the same identity. Both reject `success` / `failure_reason` inside `**kwargs` so the envelope shape stays fixed.

**Tech Stack:** Python 3.10, pytest, types.SimpleNamespace, Ruff (via `make lint`)

**Spec:** `docs/superpowers/specs/2026-07-22-team-failure-envelope-helper-design.md`

## Global Constraints

- Canonical error field is `failure_reason`; `success` is always forced to `False`.
- Empty `failure_reason` (`""`) is allowed.
- No changes to coding_team swarm mixins, `devops_team/orchestrator.py`, or code-v2 orchestrators.
- Never reference GitHub issue numbers in code, comments, docs (other than this plan/spec), or commit messages.
- Design-by-Contract: Preconditions / Postconditions on both helpers; assert contracts at boundaries.
- 90% coverage floor on touched files; `make test` and `make lint` must pass from `backend/`.

## File Structure

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/shared/team_lead_base.py` | `build_team_failure_result` + `apply_team_failure`; brief module-doc mention |
| `backend/agents/software_engineering_team/tests/test_team_lead_base.py` | Unit tests for factory + mutator |

---

### Task 1: Failure envelope helpers (TDD)

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_team_lead_base.py`
- Modify: `backend/agents/software_engineering_team/shared/team_lead_base.py`

**Interfaces:**
- Consumes: existing `copy_development_result_fields` placement / `TypeVar("T")` already in the module
- Produces:
  - `build_team_failure_result(result_cls: Callable[..., T], failure_reason: str, **partial_state: Any) -> T`
  - `apply_team_failure(result: Any, failure_reason: str, **partial_fields: Any) -> Any`

- [ ] **Step 1: Write the failing tests**

Update imports in `backend/agents/software_engineering_team/tests/test_team_lead_base.py`:

```python
from software_engineering_team.shared.team_lead_base import (
    BaseTeamLead,
    TeamLeadSharedState,
    apply_team_failure,
    build_team_failure_result,
    copy_development_result_fields,
)
```

Append these tests (near the existing `test_copy_development_result_fields_copies_all_shared_fields`):

```python
class _FakeTeamResult:
    """Minimal constructor-shaped result for factory tests (devops-like)."""

    def __init__(self, *, success: bool = True, failure_reason: str = "", **extra):
        self.success = success
        self.failure_reason = failure_reason
        for key, value in extra.items():
            setattr(self, key, value)


def test_build_team_failure_result_sets_envelope():
    result = build_team_failure_result(_FakeTeamResult, "boom")
    assert result.success is False
    assert result.failure_reason == "boom"


def test_build_team_failure_result_forwards_partial_state():
    package = {"task_id": "t1", "status": "blocked"}
    result = build_team_failure_result(
        _FakeTeamResult,
        "Quality gates failed",
        completion_package=package,
        iterations=2,
    )
    assert result.success is False
    assert result.failure_reason == "Quality gates failed"
    assert result.completion_package == package
    assert result.iterations == 2


def test_build_team_failure_result_allows_empty_failure_reason():
    result = build_team_failure_result(_FakeTeamResult, "")
    assert result.success is False
    assert result.failure_reason == ""


def test_build_team_failure_result_rejects_success_override():
    with pytest.raises(AssertionError):
        build_team_failure_result(_FakeTeamResult, "x", success=True)


def test_build_team_failure_result_rejects_failure_reason_kwarg():
    with pytest.raises(AssertionError):
        build_team_failure_result(_FakeTeamResult, "x", failure_reason="y")


def test_build_team_failure_result_rejects_non_callable_cls():
    with pytest.raises(AssertionError):
        build_team_failure_result(None, "x")  # type: ignore[arg-type]


def test_build_team_failure_result_rejects_non_str_failure_reason():
    with pytest.raises(AssertionError):
        build_team_failure_result(_FakeTeamResult, None)  # type: ignore[arg-type]


def test_apply_team_failure_mutates_in_place_and_returns_same_object():
    result = SimpleNamespace(success=True, failure_reason="", summary="keep-me", phase="setup")
    out = apply_team_failure(result, "Setup failed: disk full", phase="failed")
    assert out is result
    assert result.success is False
    assert result.failure_reason == "Setup failed: disk full"
    assert result.phase == "failed"
    assert result.summary == "keep-me"  # unrelated field preserved


def test_apply_team_failure_allows_empty_failure_reason():
    result = SimpleNamespace(success=True, failure_reason="old")
    apply_team_failure(result, "")
    assert result.success is False
    assert result.failure_reason == ""


def test_apply_team_failure_rejects_success_override():
    result = SimpleNamespace(success=True, failure_reason="")
    with pytest.raises(AssertionError):
        apply_team_failure(result, "x", success=True)


def test_apply_team_failure_rejects_failure_reason_kwarg():
    result = SimpleNamespace(success=True, failure_reason="")
    with pytest.raises(AssertionError):
        apply_team_failure(result, "x", failure_reason="y")


def test_apply_team_failure_rejects_none_result():
    with pytest.raises(AssertionError):
        apply_team_failure(None, "x")


def test_apply_team_failure_rejects_non_str_failure_reason():
    result = SimpleNamespace(success=True, failure_reason="")
    with pytest.raises(AssertionError):
        apply_team_failure(result, None)  # type: ignore[arg-type]
```

- [ ] **Step 2: Run tests to verify they fail**

From `backend/`:

```bash
python -m pytest agents/software_engineering_team/tests/test_team_lead_base.py -k "build_team_failure or apply_team_failure" -v
```

Expected: FAIL — `ImportError` / missing `build_team_failure_result` / `apply_team_failure`.

- [ ] **Step 3: Implement the helpers**

In `backend/agents/software_engineering_team/shared/team_lead_base.py`:

1. Extend the module docstring with one sentence noting the shared failure-envelope helpers (factory + mutator) usable by phase-sequential and swarm orchestrators.

2. Immediately after `copy_development_result_fields`, add:

```python
def build_team_failure_result(
    result_cls: Callable[..., T],
    failure_reason: str,
    **partial_state: Any,
) -> T:
    """Construct a failure envelope: success=False + failure_reason + optional partial state.

    Preconditions: ``result_cls`` is callable as
      ``result_cls(success=False, failure_reason=..., **partial_state)``;
      ``failure_reason`` is a str; ``partial_state`` must not include ``success``
      or ``failure_reason``.
    Postconditions: returns an instance with ``success is False`` and
      ``failure_reason`` equal to the given string; each ``partial_state`` key is
      forwarded to the constructor.
    """
    assert callable(result_cls), "result_cls must be callable"
    assert isinstance(failure_reason, str), "failure_reason must be a str"
    assert "success" not in partial_state, "success is fixed to False"
    assert "failure_reason" not in partial_state, (
        "pass failure_reason as the dedicated argument, not in kwargs"
    )
    return result_cls(success=False, failure_reason=failure_reason, **partial_state)


def apply_team_failure(
    result: Any,
    failure_reason: str,
    **partial_fields: Any,
) -> Any:
    """Mutate an existing result into the failure envelope; return the same object.

    Preconditions: ``result`` is not None and exposes assignable ``success`` /
      ``failure_reason`` attributes (and any keys in ``partial_fields``);
      ``failure_reason`` is a str; ``partial_fields`` must not include ``success``
      or ``failure_reason``.
    Postconditions: ``result.success is False``; ``result.failure_reason`` equals
      the given string; each ``partial_fields`` key is set via ``setattr``;
      returns ``result`` (same identity). Unrelated attributes are left untouched.
    """
    assert result is not None, "result is required"
    assert isinstance(failure_reason, str), "failure_reason must be a str"
    assert "success" not in partial_fields, "success is fixed to False"
    assert "failure_reason" not in partial_fields, (
        "pass failure_reason as the dedicated argument, not in kwargs"
    )
    result.success = False
    result.failure_reason = failure_reason
    for key, value in partial_fields.items():
        setattr(result, key, value)
    return result
```

Do **not** edit swarm mixins, devops, or code-v2 consumers.

- [ ] **Step 4: Run unit tests to verify they pass**

```bash
python -m pytest agents/software_engineering_team/tests/test_team_lead_base.py -v --cov=agents/software_engineering_team/shared/team_lead_base --cov-report=term-missing
```

Expected: all tests PASS; `team_lead_base.py` line coverage ≥ 90%.

- [ ] **Step 5: Lint and broader SE-team sanity check**

From `backend/`:

```bash
make lint
python -m pytest agents/software_engineering_team/tests/test_team_lead_base.py -q
```

Expected: lint clean; tests PASS.

- [ ] **Step 6: Commit**

```bash
git add \
  backend/agents/software_engineering_team/shared/team_lead_base.py \
  backend/agents/software_engineering_team/tests/test_team_lead_base.py
git commit -m "$(cat <<'EOF'
Refactor: add shared team failure envelope helpers

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Factory `build_team_failure_result` | Task 1 |
| Mutator `apply_team_failure` | Task 1 |
| `success` forced False; kwargs reject overrides | Task 1 |
| Empty `failure_reason` allowed | Task 1 |
| Partial-state forwarding / unrelated-field preserve | Task 1 |
| Unit tests + 90% coverage | Task 1 |
| No consumer migrations | Task 1 (explicit non-touch) |
