# Align `_is_real_value` With Non-Empty Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `_is_real_value` return `True` only for non-empty, non-placeholder strings, without relying on `""` being a member of `MISSION_PLACEHOLDERS`.

**Architecture:** Keep `MISSION_PLACEHOLDERS` unchanged. Change `_is_real_value` to strip, require a non-empty result, then reject known placeholders. Prove the contract with a regression test that temporarily removes `""` from the placeholder set so emptiness cannot be papered over by sentinel membership.

**Tech Stack:** Python 3.10+, pytest, ruff; branding_team helpers in `api/state.py`.

**Spec:** `docs/superpowers/specs/2026-08-07-is-real-value-empty-fix-design.md`

## Global Constraints

- Do not change `MISSION_PLACEHOLDERS` contents (still includes `""`).
- Do not refactor conversation/mission-validation flow beyond `_is_real_value`.
- Never reference GitHub issue numbers in code, comments, or commit messages.
- Design by Contract: keep existing `_is_real_value` docstring `Preconditions:` / `Postconditions:` accurate; do not weaken them.
- Work exclusively in `.worktrees/3428-is-real-value-empty-fix` on branch `3428-is-real-value-empty-fix`.
- Run verification from the worktree's `backend/` using the main-repo venv:
  `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest …`
  with cwd set to the worktree `backend/`. Prefer `PYTHONPATH=agents` if imports fail.

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/branding_team/api/state.py` | `_is_real_value` predicate (and thin callers that already use it) |
| `backend/agents/branding_team/tests/test_branding_mission_fields.py` | Unit coverage for `_is_real_value` and mission completeness helpers |

No new modules.

---

### Task 1: Failing regression tests for emptiness vs placeholders

**Files:**
- Modify: `backend/agents/branding_team/tests/test_branding_mission_fields.py` (replace/extend the `_is_real_value` tests near `test_is_real_value_none_and_whitespace_are_not_real`)

**Interfaces:**
- Consumes: `branding_team.api.state._is_real_value`, `branding_team.api.state._mission_has_brand_name`, `branding_team.api.state._mission_has_minimal_required_fields`, `branding_team.models.MISSION_PLACEHOLDERS`, `branding_team.models.BrandingMission`
- Produces: tests that fail on current `main` when `""` is removed from placeholders, and that pin empty/`None`/whitespace/placeholder/real cases

- [ ] **Step 1: Replace the thin empty/whitespace test with full contract coverage**

Replace `test_is_real_value_none_and_whitespace_are_not_real` with these three tests (keep `test_mission_placeholders_tuple_contents` and `test_default_mission_and_detection_use_shared_placeholders` unchanged):

```python
def test_is_real_value_rejects_missing_empty_whitespace_and_placeholders() -> None:
    from branding_team.api.state import _is_real_value
    from branding_team.models import MISSION_PLACEHOLDERS

    assert _is_real_value(None) is False
    assert _is_real_value("") is False
    assert _is_real_value("   ") is False
    for sentinel in MISSION_PLACEHOLDERS:
        assert _is_real_value(sentinel) is False
    assert _is_real_value("Acme Corp") is True


def test_is_real_value_empty_does_not_depend_on_empty_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emptiness must be rejected even when '' is not a placeholder sentinel."""
    from branding_team.api import state
    from branding_team.api.state import _is_real_value
    from branding_team.models import MISSION_PLACEHOLDERS

    without_empty = tuple(p for p in MISSION_PLACEHOLDERS if p != "")
    assert "" not in without_empty
    monkeypatch.setattr(state, "MISSION_PLACEHOLDERS", without_empty)

    assert _is_real_value(None) is False
    assert _is_real_value("") is False
    assert _is_real_value("   ") is False
    assert _is_real_value("Acme Corp") is True


def test_mission_completeness_helpers_reject_empty_fields() -> None:
    from branding_team.api.state import (
        _mission_has_brand_name,
        _mission_has_minimal_required_fields,
    )
    from branding_team.models import BrandingMission

    empty_mission = BrandingMission.model_construct(
        company_name="",
        company_description="",
        target_audience="",
    )
    assert _mission_has_brand_name(empty_mission) is False
    assert _mission_has_minimal_required_fields(empty_mission) is False

    real_mission = BrandingMission(
        company_name="Acme Corp",
        company_description="We build widgets for makers worldwide.",
        target_audience="Independent makers",
    )
    assert _mission_has_brand_name(real_mission) is True
    assert _mission_has_minimal_required_fields(real_mission) is True
```

- [ ] **Step 2: Run the new tests and confirm the regression fails**

Run:

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3428-is-real-value-empty-fix/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/branding_team/tests/test_branding_mission_fields.py::test_is_real_value_rejects_missing_empty_whitespace_and_placeholders \
  agents/branding_team/tests/test_branding_mission_fields.py::test_is_real_value_empty_does_not_depend_on_empty_placeholder \
  agents/branding_team/tests/test_branding_mission_fields.py::test_mission_completeness_helpers_reject_empty_fields \
  -v
```

Expected:
- `test_is_real_value_rejects_missing_empty_whitespace_and_placeholders` — PASS (today `""` is still in `MISSION_PLACEHOLDERS`)
- `test_is_real_value_empty_does_not_depend_on_empty_placeholder` — FAIL (`None`/`""`/`"   "` incorrectly return `True` when `""` is removed from placeholders)
- `test_mission_completeness_helpers_reject_empty_fields` — PASS under current sentinel coupling

Do not proceed until the middle test fails for the reason above.

- [ ] **Step 3: Commit the failing tests**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3428-is-real-value-empty-fix
git add backend/agents/branding_team/tests/test_branding_mission_fields.py
git commit -m "$(cat <<'EOF'
Add regression tests requiring _is_real_value to reject empty without empty sentinel.

EOF
)"
```

---

### Task 2: Implement contract-aligned `_is_real_value`

**Files:**
- Modify: `backend/agents/branding_team/api/state.py:133-142` (`_is_real_value` body only)

**Interfaces:**
- Consumes: `MISSION_PLACEHOLDERS`, `Optional[str]`
- Produces: `_is_real_value(value: Optional[str]) -> bool` with postcondition: `True` iff stripped value is non-empty and not in `MISSION_PLACEHOLDERS`

- [ ] **Step 1: Update `_is_real_value` implementation**

Replace the body of `_is_real_value` (keep the existing docstring) with:

```python
def _is_real_value(value: Optional[str]) -> bool:
    """True when *value* is a real (non-placeholder) string.

    Preconditions:
        ``value`` is a string or None.
    Postconditions:
        Returns True iff the stripped value is non-empty and not one of the
        known placeholder sentinels (``MISSION_PLACEHOLDERS``).
    """
    stripped = (value or "").strip()
    return bool(stripped) and stripped not in MISSION_PLACEHOLDERS
```

Do not edit `_mission_has_brand_name` or `_mission_has_minimal_required_fields`.

- [ ] **Step 2: Re-run the focused tests**

Run the same three pytest selectors from Task 1 Step 2.

Expected: all three PASS.

Also run the surrounding mission-field suite:

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3428-is-real-value-empty-fix/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/branding_team/tests/test_branding_mission_fields.py -q
```

Expected: all tests PASS.

- [ ] **Step 3: Lint touched files**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3428-is-real-value-empty-fix/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m ruff check \
  agents/branding_team/api/state.py \
  agents/branding_team/tests/test_branding_mission_fields.py
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m ruff format --check \
  agents/branding_team/api/state.py \
  agents/branding_team/tests/test_branding_mission_fields.py
```

Expected: exit code 0 for both.

- [ ] **Step 4: Commit the fix**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3428-is-real-value-empty-fix
git add backend/agents/branding_team/api/state.py
git commit -m "$(cat <<'EOF'
Make _is_real_value require a non-empty stripped value before placeholder checks.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Explicit `bool(stripped) and … not in MISSION_PLACEHOLDERS` | Task 2 |
| Leave `MISSION_PLACEHOLDERS` unchanged | Global constraint + Task 1 keeps tuple assertion |
| Cover `None`, `''`, whitespace, placeholders, real values | Task 1 |
| Verify `_mission_has_brand_name` / `_mission_has_minimal_required_fields` | Task 1 caller test |
| No lint regressions | Task 2 Step 3 |
| Out of scope: no placeholder-set edits / no broader refactor | Global constraints |
