# Design: Derive `UpdateBrandRequest` from optionalized `BrandingMissionFields`

**Branch / worktree:** `refactor/2056-update-brand-request`  
**Date:** 2026-07-24

## Problem

`UpdateBrandRequest` in `backend/agents/branding_team/api/models.py` hand-redeclares
the eight shared branding mission fields as `Optional`, with constraints that must
stay aligned with `BrandingMissionFields`. `CreateBrandRequest` already subclasses
`BrandingMissionFields` for the required create path; the update DTO still duplicates
fields and can drift.

Partial-update semantics are load-bearing: omitted fields must remain `None` so
`update_brand`’s `model_dump(exclude_none=True, exclude={"status", "name"})` only
overwrites supplied values. Carrying create-path defaults (`[]`,
`"clear, confident, human"`) onto the update DTO would silently wipe or rewrite
mission data on empty PUTs.

## Goal

Generate an all-Optional partial of `BrandingMissionFields` in the API models
module and have `UpdateBrandRequest` subclass it, keeping `name` / `status` as
API-only extras and preserving today’s partial-update behavior exactly.

## Non-goals

- No changes to `CreateBrandRequest`, `RunBrandingTeamRequest`, or
  `_mission_from_payload`.
- No behavior changes in `update_brand` / the branding store.
- No moving the optionalize helper into domain `branding_team.models`.

## Design

### File touched (primary)

`backend/agents/branding_team/api/models.py`

### `_optionalize_model`

Private helper in the same module:

- Input: a Pydantic `BaseModel` subclass (here `BrandingMissionFields`) and a
  generated class `name`.
- For each entry in `base.model_fields`, emit a field whose annotation is
  `Optional[<original annotation>]` (unwrap existing `Optional` / unions that
  already include `None` so we do not nest `Optional[Optional[...]]`), default
  `None`, and Field metadata needed for validation (notably `min_length`).
- Do **not** copy create-path defaults (`default_factory=list`, string defaults).
- Build via `pydantic.create_model`.

### `UpdateBrandRequest`

```python
_BrandingMissionFieldsPartial = _optionalize_model(
    BrandingMissionFields, name="_BrandingMissionFieldsPartial"
)

class UpdateBrandRequest(_BrandingMissionFieldsPartial):
    name: Optional[str] = Field(None, min_length=1)
    status: Optional[str] = None
```

`update_brand` in `api/routes/brands.py` stays unchanged.

### Why generated (not hand-written Optional twin)

Constraints (`min_length` on `company_name` / `company_description` /
`target_audience`) stay sourced from `BrandingMissionFields.model_fields`, so
future base edits flow into the update DTO without a second hand copy.

## Testing

Extend `backend/agents/branding_team/tests/test_branding_mission_fields.py`:

1. Shared field names present on `UpdateBrandRequest`; each is Optional with
   default `None` (empty instance dumps mission keys as `None`).
2. Extras `name` and `status` remain on the model.
3. Supplied-but-too-short `company_name` still raises `ValidationError`.
4. Existing API tests `test_put_brand_update` and
   `test_update_brand_unchanged_mission_preserves_output` pass unchanged.

Lint: `ruff` on touched files; coverage floor unchanged for modified modules.

## Success criteria

1. Mission fields on `UpdateBrandRequest` come from the generated partial, not
   hand redeclarations.
2. Partial-update semantics identical (`exclude_none` path unchanged).
3. New composition tests + existing brand-update API tests green.
4. `CreateBrandRequest` / `RunBrandingTeamRequest` / domain models untouched
   except via the existing shared base import already in use.
