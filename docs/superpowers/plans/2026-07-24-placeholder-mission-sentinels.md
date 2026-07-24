# Shared Mission Placeholder Sentinels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define branding mission placeholder sentinels once in `models.py` and have `_default_mission()` and `_is_real_value` both consume that definition.

**Architecture:** Add named string constants plus a composed `MISSION_PLACEHOLDERS` tuple next to `BrandingMissionFields` in `branding_team/models.py`. Retarget `assistant/store.py` and `api/state.py` to import those symbols. Values and detection behavior stay identical.

**Tech Stack:** Python 3.10, Pydantic models, pytest, ruff (via `make lint`).

## Global Constraints

- Behavior-preserving only: sentinel strings remain exactly `"TBD"`, `"To be discussed."`, `"—"`, and `""`.
- Do not create a new `constants.py` module; place symbols in `backend/agents/branding_team/models.py`.
- Do not couple `assistant/store` to `api/state` (or the reverse) for these literals.
- Do not change test assertion string literals unless a test fails after the retarget.
- Never reference GitHub issue numbers in source, comments, docs, or commit messages (PR body may use `Closes #2036`).
- Work in worktree `.worktrees/fix-2036-placeholder-mission-sentinels` on branch `fix/2036-placeholder-mission-sentinels`.
- Run pytest via the repo venv from the worktree `backend/` directory:
  `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest`

## File map

| Path | Role |
|---|---|
| `backend/agents/branding_team/models.py` | Owns `MISSION_PLACEHOLDER_TBD`, `MISSION_PLACEHOLDER_TO_BE_DISCUSSED`, `MISSION_PLACEHOLDERS` |
| `backend/agents/branding_team/assistant/store.py` | `_default_mission()` uses the two named string constants |
| `backend/agents/branding_team/api/state.py` | Drops local `_MISSION_PLACEHOLDERS`; imports `MISSION_PLACEHOLDERS` |
| `backend/agents/branding_team/tests/test_branding_mission_fields.py` | Locks tuple composition + default-mission / detection wiring |

**Spec:** `docs/superpowers/specs/2026-07-24-placeholder-mission-sentinels-design.md`

---

### Task 1: Add shared placeholder constants + locking tests

**Files:**
- Modify: `backend/agents/branding_team/models.py` (insert before `class BrandingMissionFields`, ~line 85)
- Modify: `backend/agents/branding_team/tests/test_branding_mission_fields.py` (append new tests)

**Interfaces:**
- Consumes: existing `BrandingMission` / `BrandingMissionFields` layout in `models.py`
- Produces:
  - `MISSION_PLACEHOLDER_TBD: str` (`"TBD"`)
  - `MISSION_PLACEHOLDER_TO_BE_DISCUSSED: str` (`"To be discussed."`)
  - `MISSION_PLACEHOLDERS: tuple[str, ...]` composed from the named strings plus `"—"` and `""`

- [ ] **Step 1: Write the failing locking tests**

Append to `backend/agents/branding_team/tests/test_branding_mission_fields.py`:

```python
def test_mission_placeholders_tuple_contents() -> None:
    from branding_team.models import (
        MISSION_PLACEHOLDER_TBD,
        MISSION_PLACEHOLDER_TO_BE_DISCUSSED,
        MISSION_PLACEHOLDERS,
    )

    assert MISSION_PLACEHOLDER_TBD == "TBD"
    assert MISSION_PLACEHOLDER_TO_BE_DISCUSSED == "To be discussed."
    assert MISSION_PLACEHOLDERS == (
        MISSION_PLACEHOLDER_TBD,
        MISSION_PLACEHOLDER_TO_BE_DISCUSSED,
        "—",
        "",
    )


def test_default_mission_and_detection_use_shared_placeholders() -> None:
    from branding_team.api.state import _is_real_value
    from branding_team.assistant.store import _default_mission
    from branding_team.models import (
        MISSION_PLACEHOLDER_TBD,
        MISSION_PLACEHOLDER_TO_BE_DISCUSSED,
        MISSION_PLACEHOLDERS,
    )

    mission = _default_mission()
    assert mission.company_name == MISSION_PLACEHOLDER_TBD
    assert mission.company_description == MISSION_PLACEHOLDER_TO_BE_DISCUSSED
    assert mission.target_audience == MISSION_PLACEHOLDER_TBD
    for sentinel in MISSION_PLACEHOLDERS:
        assert _is_real_value(sentinel) is False
    assert _is_real_value("Acme Corp") is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2036-placeholder-mission-sentinels/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/branding_team/tests/test_branding_mission_fields.py::test_mission_placeholders_tuple_contents \
  agents/branding_team/tests/test_branding_mission_fields.py::test_default_mission_and_detection_use_shared_placeholders \
  -v
```

Expected: FAIL with `ImportError` / `cannot import name 'MISSION_PLACEHOLDER_TBD'` (or similar) because the constants do not exist yet.

- [ ] **Step 3: Add the constants to `models.py`**

Immediately before `class BrandingMissionFields` (after the Shared models section header / preceding types), insert:

```python
# Sentinel strings for mission fields that have no real value yet.
# Used by default-mission construction and placeholder detection.
MISSION_PLACEHOLDER_TBD = "TBD"
MISSION_PLACEHOLDER_TO_BE_DISCUSSED = "To be discussed."
MISSION_PLACEHOLDERS = (
    MISSION_PLACEHOLDER_TBD,
    MISSION_PLACEHOLDER_TO_BE_DISCUSSED,
    "—",
    "",
)
```

Do **not** retarget `store.py` / `state.py` in this step — leave that for Task 2. After this step only `test_mission_placeholders_tuple_contents` should be able to pass; `test_default_mission_and_detection_use_shared_placeholders` may still pass on string equality (literals match) even before wiring, because `_default_mission` still hardcodes the same values and `_MISSION_PLACEHOLDERS` still contains the same strings. That is expected; Task 2 removes the duplicate literals so the shared symbols are the sole definition sites.

- [ ] **Step 4: Re-run the locking tests**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2036-placeholder-mission-sentinels/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/branding_team/tests/test_branding_mission_fields.py::test_mission_placeholders_tuple_contents \
  agents/branding_team/tests/test_branding_mission_fields.py::test_default_mission_and_detection_use_shared_placeholders \
  -v
```

Expected: both PASS (equality holds via identical literal values even before call-site retarget).

- [ ] **Step 5: Commit**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2036-placeholder-mission-sentinels
git add backend/agents/branding_team/models.py \
  backend/agents/branding_team/tests/test_branding_mission_fields.py
git commit -m "$(cat <<'EOF'
Add shared branding mission placeholder sentinel constants.

EOF
)"
```

Only commit if the user asked for commits in this session; otherwise stop after Step 4 and report the diff.

---

### Task 2: Retarget call sites to the shared constants

**Files:**
- Modify: `backend/agents/branding_team/assistant/store.py` (imports + `_default_mission`, ~lines 24–36)
- Modify: `backend/agents/branding_team/api/state.py` (imports + remove `_MISSION_PLACEHOLDERS` + `_is_real_value` docstring, ~lines 25 and 128–140)

**Interfaces:**
- Consumes: `MISSION_PLACEHOLDER_TBD`, `MISSION_PLACEHOLDER_TO_BE_DISCUSSED`, `MISSION_PLACEHOLDERS` from Task 1
- Produces: no new public API; private `_default_mission` / `_is_real_value` behavior unchanged

- [ ] **Step 1: Update `assistant/store.py` imports and `_default_mission`**

Change the models import from:

```python
from ..models import BrandingMission, TeamOutput
```

to:

```python
from ..models import (
    BrandingMission,
    MISSION_PLACEHOLDER_TBD,
    MISSION_PLACEHOLDER_TO_BE_DISCUSSED,
    TeamOutput,
)
```

Replace `_default_mission` with:

```python
def _default_mission() -> BrandingMission:
    return BrandingMission(
        company_name=MISSION_PLACEHOLDER_TBD,
        company_description=MISSION_PLACEHOLDER_TO_BE_DISCUSSED,
        target_audience=MISSION_PLACEHOLDER_TBD,
    )
```

- [ ] **Step 2: Update `api/state.py` to import and use `MISSION_PLACEHOLDERS`**

Change the models import from:

```python
from branding_team.models import BrandingMission, BrandPhase, TeamOutput
```

to:

```python
from branding_team.models import (
    BrandingMission,
    BrandPhase,
    MISSION_PLACEHOLDERS,
    TeamOutput,
)
```

Delete the local definition and its comment:

```python
# Sentinel strings the assistant/UI use for a field that has no real value yet.
_MISSION_PLACEHOLDERS = ("TBD", "To be discussed.", "—", "")
```

Update `_is_real_value` so the membership check and docstring reference the shared name:

```python
def _is_real_value(value: Optional[str]) -> bool:
    """True when *value* is a real (non-placeholder) string.

    Preconditions:
        ``value`` is a string or None.
    Postconditions:
        Returns True iff the stripped value is non-empty and not one of the
        known placeholder sentinels (``MISSION_PLACEHOLDERS``).
    """
    return (value or "").strip() not in MISSION_PLACEHOLDERS
```

- [ ] **Step 3: Confirm no remaining local duplicate literals at the call sites**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2036-placeholder-mission-sentinels
rg -n '_MISSION_PLACEHOLDERS|company_name="TBD"|To be discussed' \
  backend/agents/branding_team/assistant/store.py \
  backend/agents/branding_team/api/state.py
```

Expected: no matches in those two files.

- [ ] **Step 4: Run locking tests + branding regression suite**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2036-placeholder-mission-sentinels/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/branding_team/tests/test_branding_mission_fields.py \
  agents/branding_team/tests/test_conversation_store.py \
  agents/branding_team/tests/test_api.py \
  agents/branding_team/tests/test_conversation_flow.py \
  agents/branding_team/tests/test_assistant.py \
  -q
```

Expected: all PASS.

- [ ] **Step 5: Lint**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2036-placeholder-mission-sentinels/backend
make lint
```

Expected: ruff check + format clean for the touched files.

- [ ] **Step 6: Commit**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2036-placeholder-mission-sentinels
git add backend/agents/branding_team/assistant/store.py \
  backend/agents/branding_team/api/state.py
git commit -m "$(cat <<'EOF'
Retarget mission default and placeholder detection to shared sentinels.

EOF
)"
```

Only commit if the user asked for commits in this session; otherwise stop after Step 5 and report the diff.

---

## Plan self-review

1. **Spec coverage:** Shared full tuple in `models.py` → Task 1. Both call sites retargeted → Task 2. Values/behavior unchanged → Global Constraints + Steps that keep exact strings. Existing tests unchanged except additive locking tests → Task 1 append-only. Lint/pytest → Task 2 Steps 4–5.
2. **Placeholders:** None; all steps include exact code and commands.
3. **Type consistency:** Constant names match across tasks (`MISSION_PLACEHOLDER_TBD`, `MISSION_PLACEHOLDER_TO_BE_DISCUSSED`, `MISSION_PLACEHOLDERS`).
