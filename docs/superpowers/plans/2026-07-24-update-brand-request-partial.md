# Derive `UpdateBrandRequest` from Optionalized Mission Fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hand-declared optional mission fields on `UpdateBrandRequest` with a generated all-Optional partial of `BrandingMissionFields`, preserving partial-update semantics.

**Architecture:** A private `_optionalize_model` helper in `api/models.py` builds `_BrandingMissionFieldsPartial` via `pydantic.create_model`, wrapping each shared field as `Optional[...] = None` while copying validation constraints (`min_length`). `UpdateBrandRequest` subclasses that partial and keeps only `name` / `status` extras. `update_brand` is unchanged.

**Tech Stack:** Python 3.10+, Pydantic v2 (`BaseModel`, `Field`, `create_model`, `model_fields`).

**Spec:** `docs/superpowers/specs/2026-07-24-update-brand-request-partial-design.md`

## Global Constraints

- Do not change `CreateBrandRequest`, `RunBrandingTeamRequest`, `_mission_from_payload`, or `update_brand` behavior.
- Optionalized fields must default to `None` (never create-path `[]` / `"clear, confident, human"` defaults).
- Do not move the helper into domain `branding_team.models`.
- Do not mention tracker issue numbers in commit messages or source comments.
- Work only in `.worktrees/refactor-2056-update-brand-request` on branch `refactor/2056-update-brand-request`.
- Design by Contract docstrings on new public/private helpers and `UpdateBrandRequest`.

---

### Task 1: Failing composition tests for `UpdateBrandRequest`

**Files:**
- Modify: `backend/agents/branding_team/tests/test_branding_mission_fields.py`
- Test: same file

**Interfaces:**
- Consumes: Existing `SHARED_FIELD_NAMES`; current hand-written `UpdateBrandRequest`
- Produces: Failing tests that pin optional defaults, extras, and `min_length` when the implementation lands

- [ ] **Step 1: Write the failing tests**

Add imports and constants, then append these tests (keep existing CreateBrand / BrandingMission tests intact):

```python
from branding_team.api.models import CreateBrandRequest, UpdateBrandRequest

UPDATE_BRAND_EXTRA_FIELD_NAMES = (
    "name",
    "status",
)


def test_update_brand_request_includes_shared_and_extra_fields() -> None:
    names = tuple(UpdateBrandRequest.model_fields)
    for name in SHARED_FIELD_NAMES:
        assert name in names
    for name in UPDATE_BRAND_EXTRA_FIELD_NAMES:
        assert name in names


def test_update_brand_request_mission_fields_default_to_none() -> None:
    req = UpdateBrandRequest()
    dumped = req.model_dump()
    for name in SHARED_FIELD_NAMES:
        assert dumped[name] is None
    assert dumped["name"] is None
    assert dumped["status"] is None


def test_update_brand_request_rejects_short_company_name_when_supplied() -> None:
    with pytest.raises(ValidationError):
        UpdateBrandRequest(company_name="A")


def test_update_brand_request_partial_dump_excludes_none_mission_fields() -> None:
    req = UpdateBrandRequest(company_description="Updated description here")
    patch = req.model_dump(exclude_none=True, exclude={"status", "name"})
    assert patch == {"company_description": "Updated description here"}
```

Update the module docstring Postconditions line to mention `UpdateBrandRequest` as well as `CreateBrandRequest`.

- [ ] **Step 2: Run tests to verify they fail for the right reason**

Run (from worktree `backend/`, using the repo venv if present):

```bash
cd backend
PYTHONPATH=agents \
  /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/branding_team/tests/test_branding_mission_fields.py::test_update_brand_request_mission_fields_default_to_none \
  agents/branding_team/tests/test_branding_mission_fields.py::test_update_brand_request_partial_dump_excludes_none_mission_fields \
  -v
```

Expected: at least one failure that proves create-path defaults are still wrong **or** that assertions about composition are not yet met after later steps — with the **current** hand-written model, `test_update_brand_request_mission_fields_default_to_none` and `test_update_brand_request_partial_dump_excludes_none_mission_fields` should **PASS** today (behavior already correct). That is expected for TDD on a behavior-preserving refactor: the red step is adding a test that will fail **after** a wrong implementation, so instead add one composition assertion that fails until the generated base exists:

Replace / add this stronger test (this is the intentional RED for Task 1):

```python
def test_update_brand_request_mission_fields_come_from_optionalized_base() -> None:
    """Mission fields must be inherited from the generated partial, not redeclared."""
    from branding_team.api import models as api_models

    partial = api_models._BrandingMissionFieldsPartial
    assert issubclass(UpdateBrandRequest, partial)
    assert tuple(partial.model_fields) == SHARED_FIELD_NAMES
```

Re-run:

```bash
PYTHONPATH=agents \
  /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/branding_team/tests/test_branding_mission_fields.py::test_update_brand_request_mission_fields_come_from_optionalized_base \
  -v
```

Expected: FAIL with `AttributeError: module ... has no attribute '_BrandingMissionFieldsPartial'` (or `issubclass` failure if the name exists but wiring is wrong).

Keep the four behavioral tests from Step 1 as well; they should PASS on the current hand-written model and remain green through the refactor.

- [ ] **Step 3: Commit the failing/new tests**

```bash
git add backend/agents/branding_team/tests/test_branding_mission_fields.py
git commit -m "$(cat <<'EOF'
Add UpdateBrandRequest composition and partial-update pins.

EOF
)"
```

---

### Task 2: Implement `_optionalize_model` and rewire `UpdateBrandRequest`

**Files:**
- Modify: `backend/agents/branding_team/api/models.py`
- Test: `backend/agents/branding_team/tests/test_branding_mission_fields.py`
- Test: `backend/agents/branding_team/tests/test_api.py` (run only; no edits expected)

**Interfaces:**
- Consumes: `BrandingMissionFields.model_fields`; Task 1 tests
- Produces: `_optionalize_model(base: type[BaseModel], *, name: str) -> type[BaseModel]`; `_BrandingMissionFieldsPartial`; `UpdateBrandRequest(_BrandingMissionFieldsPartial)`

- [ ] **Step 1: Add imports needed by the helper**

At the top of `api/models.py`, extend imports:

```python
import types
from typing import Any, Dict, List, Optional, Union, get_args, get_origin

from pydantic import BaseModel, Field, create_model
from pydantic.fields import FieldInfo
```

(Keep existing branding_team imports.)

- [ ] **Step 2: Implement `_optionalize_model` and the partial base**

Insert **above** `UpdateBrandRequest` (after `CreateBrandRequest`):

```python
def _unwrap_noneable(annotation: Any) -> Any:
    """Return the non-None arm of ``Optional[T]`` / ``T | None``, else ``annotation``.

    Preconditions:
        - ``annotation`` is a typing annotation object.
    Postconditions:
        - If ``annotation`` is a union of exactly one non-None type and ``None``,
          return that non-None type; otherwise return ``annotation`` unchanged.
    """
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if len(args) == len(non_none) + 1 and len(non_none) == 1:
            return non_none[0]
    return annotation


def _constraint_kwargs(info: FieldInfo) -> dict[str, Any]:
    """Copy validation metadata that must survive optionalization.

    Preconditions:
        - ``info`` is a Pydantic v2 ``FieldInfo``.
    Postconditions:
        - Returned dict contains only constraint keys present on ``info`` with
          non-``None`` values from the supported set below.
    """
    out: dict[str, Any] = {}
    for key in (
        "min_length",
        "max_length",
        "ge",
        "le",
        "gt",
        "lt",
        "pattern",
        "description",
        "title",
    ):
        value = getattr(info, key, None)
        if value is not None:
            out[key] = value
    return out


def _optionalize_model(base: type[BaseModel], *, name: str) -> type[BaseModel]:
    """Build an all-Optional twin of ``base`` with defaults forced to ``None``.

    Preconditions:
        - ``base`` is a Pydantic ``BaseModel`` subclass with a non-empty
          ``model_fields`` mapping.
        - ``name`` is a non-empty Python identifier string.
    Postconditions:
        - Returned model has the same field names as ``base``.
        - Every field is annotated ``Optional[...]`` with default ``None``.
        - Create-path defaults from ``base`` are not copied.
        - Supported Field constraints (e.g. ``min_length``) are preserved.
    """
    assert issubclass(base, BaseModel)
    assert name.isidentifier()
    assert base.model_fields, "base model must declare fields"

    field_definitions: dict[str, Any] = {}
    for field_name, field_info in base.model_fields.items():
        inner = _unwrap_noneable(field_info.annotation)
        field_definitions[field_name] = (
            Optional[inner],
            Field(default=None, **_constraint_kwargs(field_info)),
        )
    return create_model(name, __base__=BaseModel, **field_definitions)


_BrandingMissionFieldsPartial = _optionalize_model(
    BrandingMissionFields, name="_BrandingMissionFieldsPartial"
)
```

- [ ] **Step 3: Replace `UpdateBrandRequest` body**

Replace the hand-declared class with:

```python
class UpdateBrandRequest(_BrandingMissionFieldsPartial):
    """Partial brand update: optionalized mission fields plus name/status extras.

    Preconditions:
        - Supplied mission string fields must satisfy the same ``min_length``
          constraints as ``BrandingMissionFields`` when not ``None``.
    Postconditions:
        - Omitted fields remain ``None`` so callers can
          ``model_dump(exclude_none=True)`` for selective overwrite.
        - ``name`` and ``status`` are API-only extras (not mission fields).
    """

    name: Optional[str] = Field(None, min_length=1)
    status: Optional[str] = None
```

- [ ] **Step 4: Run composition tests (GREEN)**

```bash
cd backend
PYTHONPATH=agents \
  /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/branding_team/tests/test_branding_mission_fields.py -v
```

Expected: all tests in that file PASS (including CreateBrand pins and new UpdateBrand pins).

- [ ] **Step 5: Run brand-update API regression tests**

```bash
PYTHONPATH=agents \
  /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/branding_team/tests/test_api.py::test_put_brand_update \
  agents/branding_team/tests/test_api.py::test_update_brand_unchanged_mission_preserves_output \
  -v
```

Expected: both PASS.

- [ ] **Step 6: Lint touched files**

```bash
cd backend
RUFF=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff
$RUFF check agents/branding_team/api/models.py agents/branding_team/tests/test_branding_mission_fields.py
$RUFF format --check agents/branding_team/api/models.py agents/branding_team/tests/test_branding_mission_fields.py
```

Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add backend/agents/branding_team/api/models.py
git commit -m "$(cat <<'EOF'
Derive UpdateBrandRequest from optionalized BrandingMissionFields.

EOF
)"
```

---

## Self-review (plan vs spec)

1. **Spec coverage:** Generated partial helper, `UpdateBrandRequest` subclass, `None` defaults, constraint copy, non-goals (no Create/Run/`update_brand` changes), composition + API tests — covered by Tasks 1–2.
2. **Placeholders:** None.
3. **Consistency:** `_BrandingMissionFieldsPartial` / `_optionalize_model` names match tests and implementation steps; extras remain `name` / `status`.
