# Simplify `_mission_from_payload` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the eight-field hand map in `_mission_from_payload` with `BrandingMission(**payload.mission_fields())`, after adding `mission_fields()` on `BrandingMissionFields`.

**Architecture:** `BrandingMissionFields.mission_fields()` dumps only the eight shared field names (via `model_dump(include=set(BrandingMissionFields.model_fields))`). Create/run request DTOs inherit that method. `_mission_from_payload` becomes a one-line delegation typed on `BrandingMissionFields`. Route call sites stay unchanged.

**Tech Stack:** Python 3.10+, Pydantic v2, pytest, ruff; branding team under `backend/agents/branding_team/`.

## Global Constraints

- Design by Contract: every new/changed public function documents `Preconditions:` / `Postconditions:` in its docstring.
- Do not mention GitHub issue numbers in code, comments, commit messages, or docs.
- No API contract changes; `UpdateBrandRequest` is out of scope.
- Coverage floor: 90% line coverage on touched files; `make lint` and `make test` from `backend/` must pass.
- Work in the existing worktree at `.worktrees/issue-2071-simplify-mission-from-payload` on branch `refactor/2071-simplify-mission-from-payload`.
- Prefer the main-repo venv at `backend/.venv` when the worktree has none:  
  `PYTHON=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python`

## File map

| File | Role |
|------|------|
| `backend/agents/branding_team/models.py` | Add `mission_fields()` on `BrandingMissionFields` |
| `backend/agents/branding_team/api/state.py` | Collapse `_mission_from_payload` to one-line delegation; tighten param type |
| `backend/agents/branding_team/tests/test_branding_mission_fields.py` | Unit tests for `mission_fields()` and `_mission_from_payload` |
| `backend/agents/branding_team/api/routes/brands.py` | No edit expected (keeps calling `_mission_from_payload`) |
| `backend/agents/branding_team/api/routes/sessions.py` | No edit expected |

---

### Task 1: Add `mission_fields()` on `BrandingMissionFields`

**Files:**
- Modify: `backend/agents/branding_team/models.py` (class `BrandingMissionFields`, after the field declarations ~lines 109–117)
- Test: `backend/agents/branding_team/tests/test_branding_mission_fields.py`

**Interfaces:**
- Consumes: existing `BrandingMissionFields.model_fields` (eight shared names already pinned by `SHARED_FIELD_NAMES` in tests)
- Produces: `BrandingMissionFields.mission_fields(self) -> dict[str, Any]` — keys exactly the eight shared fields; no subclass API extras

- [ ] **Step 1: Write the failing tests**

Append to `backend/agents/branding_team/tests/test_branding_mission_fields.py` (after `test_branding_mission_fields_constructs_independently` is a good home; keep names distinct from the existing `test_mission_fields_exposes_exactly_the_eight_shared_fields` which asserts `model_fields`, not the method):

```python
def test_mission_fields_method_returns_exactly_shared_keys_and_values() -> None:
    fields = BrandingMissionFields(
        company_name="Acme",
        company_description="We build widgets for teams",
        target_audience="B2B buyers",
        values=["clarity"],
        differentiators=["speed"],
        desired_voice="warm",
        existing_brand_material=["logo.svg"],
        wiki_path="/wiki/acme",
    )
    dumped = fields.mission_fields()
    assert tuple(dumped.keys()) == SHARED_FIELD_NAMES
    assert dumped == {
        "company_name": "Acme",
        "company_description": "We build widgets for teams",
        "target_audience": "B2B buyers",
        "values": ["clarity"],
        "differentiators": ["speed"],
        "desired_voice": "warm",
        "existing_brand_material": ["logo.svg"],
        "wiki_path": "/wiki/acme",
    }


def test_mission_fields_method_omits_create_brand_api_extras() -> None:
    req = CreateBrandRequest(
        company_name="Acme",
        company_description="We build widgets for teams",
        target_audience="B2B buyers",
        name="Display Name",
        conversation_id="conv-1",
    )
    dumped = req.mission_fields()
    assert tuple(dumped.keys()) == SHARED_FIELD_NAMES
    assert "name" not in dumped
    assert "conversation_id" not in dumped


def test_mission_fields_method_omits_run_request_api_extras() -> None:
    req = RunBrandingTeamRequest(
        company_name="Acme",
        company_description="We build widgets for teams",
        target_audience="B2B buyers",
        human_approved=True,
        client_id="c1",
        brand_id="b1",
        target_phase="strategic_core",
    )
    dumped = req.mission_fields()
    assert tuple(dumped.keys()) == SHARED_FIELD_NAMES
    assert "human_approved" not in dumped
    assert "client_id" not in dumped
    assert "brand_id" not in dumped
    assert "target_phase" not in dumped
    assert "brand_checks" not in dumped
    assert "human_feedback" not in dumped
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from `backend/` in the worktree):

```bash
PYTHON=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python \
  $PYTHON -m pytest agents/branding_team/tests/test_branding_mission_fields.py::test_mission_fields_method_returns_exactly_shared_keys_and_values \
  agents/branding_team/tests/test_branding_mission_fields.py::test_mission_fields_method_omits_create_brand_api_extras \
  agents/branding_team/tests/test_branding_mission_fields.py::test_mission_fields_method_omits_run_request_api_extras -v
```

Expected: FAIL with `AttributeError: 'BrandingMissionFields' object has no attribute 'mission_fields'` (or equivalent on the subclass instances).

- [ ] **Step 3: Implement `mission_fields()`**

In `backend/agents/branding_team/models.py`, on `BrandingMissionFields` after the field declarations and before `BrandingMission`, add:

```python
    def mission_fields(self) -> dict[str, Any]:
        """Return only the eight shared mission fields as a plain dict.

        Preconditions:
            ``self`` is a valid ``BrandingMissionFields`` instance (or subclass).
        Postconditions:
            Returns a dict whose keys are exactly the eight shared mission field
            names from ``BrandingMissionFields.model_fields``; values match
            ``self``; API-only extras declared on subclasses are omitted.
        """
        return self.model_dump(include=set(BrandingMissionFields.model_fields))
```

Critical: `include=` must reference **`BrandingMissionFields.model_fields`**, not `type(self).model_fields`, so subclass extras never leak.

`Any` is already imported in `models.py`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHON=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python \
  $PYTHON -m pytest agents/branding_team/tests/test_branding_mission_fields.py::test_mission_fields_method_returns_exactly_shared_keys_and_values \
  agents/branding_team/tests/test_branding_mission_fields.py::test_mission_fields_method_omits_create_brand_api_extras \
  agents/branding_team/tests/test_branding_mission_fields.py::test_mission_fields_method_omits_run_request_api_extras \
  agents/branding_team/tests/test_branding_mission_fields.py -q
```

Expected: all three new tests PASS; full file PASS (existing tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add backend/agents/branding_team/models.py \
  backend/agents/branding_team/tests/test_branding_mission_fields.py
git commit -m "$(cat <<'EOF'
Add BrandingMissionFields.mission_fields for shared-field dumps.

EOF
)"
```

---

### Task 2: Collapse `_mission_from_payload` to one-line delegation

**Files:**
- Modify: `backend/agents/branding_team/api/state.py` (`_mission_from_payload`, ~lines 158–180; update imports)
- Test: `backend/agents/branding_team/tests/test_branding_mission_fields.py`

**Interfaces:**
- Consumes: `BrandingMissionFields.mission_fields() -> dict[str, Any]` from Task 1
- Produces: `_mission_from_payload(payload: BrandingMissionFields) -> BrandingMission` as `return BrandingMission(**payload.mission_fields())`

- [ ] **Step 1: Write the failing/locking test for the helper**

Append to `test_branding_mission_fields.py`:

```python
def test_mission_from_payload_builds_mission_from_shared_fields_only() -> None:
    from branding_team.api.state import _mission_from_payload

    req = CreateBrandRequest(
        company_name="Acme",
        company_description="We build widgets for teams",
        target_audience="B2B buyers",
        values=["clarity"],
        name="Display Name",
        conversation_id="conv-1",
    )
    mission = _mission_from_payload(req)
    assert isinstance(mission, BrandingMission)
    assert mission.company_name == "Acme"
    assert mission.company_description == "We build widgets for teams"
    assert mission.target_audience == "B2B buyers"
    assert mission.values == ["clarity"]
    assert mission.desired_voice == "clear, confident, human"
    assert mission.visual_style == ""
    assert mission.color_inspiration == []
    assert mission.selected_palette_index is None
    assert not hasattr(mission, "name") or "name" not in mission.model_fields
```

(Use `assert "name" not in BrandingMission.model_fields` if preferred — cleaner than `hasattr`.)

Preferred assertion for extras:

```python
    assert "name" not in BrandingMission.model_fields
    assert "conversation_id" not in BrandingMission.model_fields
```

- [ ] **Step 2: Run the new test (should still pass against the old hand-map)**

```bash
PYTHON=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python \
  $PYTHON -m pytest agents/branding_team/tests/test_branding_mission_fields.py::test_mission_from_payload_builds_mission_from_shared_fields_only -v
```

Expected: PASS against the current field-by-field implementation (characterization). If it fails, fix the test before changing production code.

- [ ] **Step 3: Replace `_mission_from_payload` body and typing**

In `backend/agents/branding_team/api/state.py`:

1. Change the import from `branding_team.models` to include `BrandingMissionFields`:

```python
from branding_team.models import (
    MISSION_PLACEHOLDERS,
    BrandingMission,
    BrandingMissionFields,
    BrandPhase,
    TeamOutput,
)
```

2. Replace `_mission_from_payload` with:

```python
def _mission_from_payload(payload: BrandingMissionFields) -> BrandingMission:
    """Build a ``BrandingMission`` from a create/run request payload.

    Preconditions:
        ``payload`` is a ``BrandingMissionFields`` instance (satisfied by
        ``CreateBrandRequest`` and ``RunBrandingTeamRequest``).
    Postconditions:
        Returns a ``BrandingMission`` built from ``payload.mission_fields()``;
        visual-identity fields use ``BrandingMission`` defaults; performs no
        I/O and does not mutate ``payload``.
    """
    return BrandingMission(**payload.mission_fields())
```

Remove the unused `Any` import from `state.py` **only if** nothing else in the file still needs it. Check: `typing` currently imports `Any, List, Optional` — `Any` may become unused after this change; drop it if ruff reports F401.

Do **not** change `brands.py` or `sessions.py` call sites.

- [ ] **Step 4: Run branding unit tests**

```bash
PYTHON=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python \
  $PYTHON -m pytest agents/branding_team/tests/test_branding_mission_fields.py -q
```

Expected: all PASS.

Also run API/assistant suites (may skip without Postgres; non-skipped must pass):

```bash
PYTHON=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python \
  $PYTHON -m pytest agents/branding_team/tests/test_api.py agents/branding_team/tests/test_assistant.py -q
```

Expected: PASS or SKIP only; zero failures.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/branding_team/api/state.py \
  backend/agents/branding_team/tests/test_branding_mission_fields.py
git commit -m "$(cat <<'EOF'
Simplify branding _mission_from_payload to mission_fields delegation.

EOF
)"
```

---

### Task 3: Lint, full test gate, coverage check

**Files:**
- Verify only (no production edits unless lint/tests demand fixes)

**Interfaces:**
- Consumes: Task 1 + Task 2 deliverables
- Produces: clean `make lint` / branding pytest + coverage evidence for touched files

- [ ] **Step 1: Run ruff**

From worktree `backend/`:

```bash
cd backend
make lint
```

If the worktree venv is missing, either symlink/use the main venv or run:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff check agents/branding_team
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff format --check agents/branding_team
```

Expected: clean (exit 0).

- [ ] **Step 2: Run full backend test target if feasible; otherwise branding suite with coverage**

Preferred:

```bash
make test
```

If that is too heavy for the session environment, at minimum:

```bash
PYTHON=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python \
  $PYTHON -m pytest agents/branding_team/tests/ \
    --cov=agents/branding_team/models \
    --cov=agents/branding_team/api/state \
    --cov-report=term-missing -q
```

Expected: no failures; line coverage on the touched modules stays ≥ 90% (new `mission_fields` and the one-line helper must be covered by Task 1–2 tests).

- [ ] **Step 3: Commit any lint-only fixes if needed**

Only if Step 1–2 required file changes:

```bash
git add -u backend/agents/branding_team
git commit -m "$(cat <<'EOF'
Fix lint after mission_fields payload simplification.

EOF
)"
```

If nothing to commit, skip.

---

## Spec coverage checklist

| Spec requirement | Task |
|------------------|------|
| `mission_fields()` on `BrandingMissionFields` dumping eight shared fields | Task 1 |
| `_mission_from_payload` → `BrandingMission(**payload.mission_fields())` | Task 2 |
| Call sites keep using the helper | Task 2 (explicit non-edit) |
| Type tighten away from `Any` | Task 2 |
| Unit tests for keys/values/extras omitted | Task 1 |
| Helper builds mission with visual defaults | Task 2 |
| `test_api` / `test_assistant` unchanged & pass | Task 2–3 |
| `make lint` / coverage floor | Task 3 |
| `UpdateBrandRequest` / further DTOs out of scope | Global Constraints + File map |

## Self-review notes

- No placeholders left in steps.
- Method name `mission_fields` is consistent across models, helper, and tests.
- Include-set uses the **base** class field map to avoid subclass extras.
- Existing test `test_mission_fields_exposes_exactly_the_eight_shared_fields` is left alone (it tests `model_fields`, not the new method).
