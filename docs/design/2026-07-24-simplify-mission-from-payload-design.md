# Design: Simplify `_mission_from_payload` after DTO migration

Date: 2026-07-24

## Context

`CreateBrandRequest`, `UpdateBrandRequest`, and `RunBrandingTeamRequest` already derive shared mission fields from `BrandingMissionFields`. `BrandingMission` also subclasses that base. The remaining hand-written bridge `_mission_from_payload` in `backend/agents/branding_team/api/state.py` still copies eight fields one-by-one from create/run request payloads into `BrandingMission`. That mapping is redundant now that the field shapes align.

## Goal

Reduce `_mission_from_payload` to a one-line delegation with no manual field-by-field mapping, via a shared `mission_fields()` helper on `BrandingMissionFields`.

## Non-goals

- Further DTO redesign beyond consuming the completed shared-base migrations
- Changes to `UpdateBrandRequest` partial-update semantics or shape
- API contract changes visible to clients
- Placeholder-sentinel consolidation (already handled under the parent epic)

## Architecture

1. Add `mission_fields()` on `BrandingMissionFields` in `backend/agents/branding_team/models.py`.
2. Keep `_mission_from_payload` in `api/state.py` as a thin shared helper used by existing call sites.
3. Leave route modules (`api/routes/brands.py`, `api/routes/sessions.py`) calling `_mission_from_payload` unchanged aside from any import/typing fallout (none expected).

`UpdateBrandRequest` remains an optionalized twin of the shared fields and is not a `_mission_from_payload` caller; it does not need `mission_fields()` for this change.

## Components

### `BrandingMissionFields.mission_fields()`

- **Location:** `backend/agents/branding_team/models.py`
- **Behavior:** `return self.model_dump(include=set(BrandingMissionFields.model_fields))`
- **Preconditions:** Called on a valid `BrandingMissionFields` instance (or subclass).
- **Postconditions:** Returns a `dict` whose keys are exactly the eight shared mission field names; values match the instance; API-only extras on subclasses (`name`, `conversation_id`, `brand_checks`, etc.) are absent.
- Inherited automatically by `CreateBrandRequest`, `RunBrandingTeamRequest`, and `BrandingMission`.

### `_mission_from_payload`

- **Location:** `backend/agents/branding_team/api/state.py`
- **Body:** `return BrandingMission(**payload.mission_fields())`
- **Typing:** Prefer `BrandingMissionFields` (or an equivalent Protocol exposing `mission_fields()`) instead of `Any`, since create/run request DTOs subclass the shared base.
- **Preconditions:** `payload` exposes `mission_fields()` with the eight shared fields populated under normal FastAPI validation.
- **Postconditions:** Returns a `BrandingMission` with those shared fields copied and visual-identity fields at their model defaults; no I/O; does not mutate `payload`.

### Call sites (unchanged call pattern)

- `create_brand` in `api/routes/brands.py`
- `run_branding_team` and `create_branding_session` in `api/routes/sessions.py`

## Data flow

```
CreateBrandRequest | RunBrandingTeamRequest
        │
        ▼
_mission_from_payload(payload)
        │  BrandingMission(**payload.mission_fields())
        ▼
BrandingMission  (shared fields from payload; visual fields defaulted)
        │
        ▼
store / orchestrator
```

Invalid payloads still fail at FastAPI/Pydantic validation before the helper runs. The helper introduces no new error paths.

## Testing

- Extend `test_branding_mission_fields.py`:
  - `mission_fields()` keys are exactly the eight shared names.
  - Values match the source instance.
  - Extras from `CreateBrandRequest` / `RunBrandingTeamRequest` are absent.
- Optionally assert `_mission_from_payload` preserves shared fields and leaves visual fields at defaults (only if a nearby unit-test home already fits; otherwise rely on API coverage).
- Acceptance gates: `test_api.py` and `test_assistant.py` pass unchanged; `make test` and `make lint` from `backend/`; 90% line-coverage floor holds on touched files.

## Risks

- Dumping with `include=set(BrandingMissionFields.model_fields)` must use the **base** model’s field map (not the subclass’s) so API extras never leak into `BrandingMission(**...)`.
- Tightening `_mission_from_payload`’s parameter type must still accept both create and run request types; both already subclass `BrandingMissionFields`.
