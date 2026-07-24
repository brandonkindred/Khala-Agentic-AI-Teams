# Design: Shared Mission Placeholder Sentinels

**Status:** Approved 2026-07-24  
**Date:** 2026-07-24  
**Type:** Refactor (behavior-preserving constant consolidation)  
**Branch / worktree:** `fix/2036-placeholder-mission-sentinels` / `.worktrees/fix-2036-placeholder-mission-sentinels`

## Problem

Placeholder-mission sentinel values are hardcoded independently in two places:

- `assistant/store.py` `_default_mission()` uses `"TBD"` and `"To be discussed."` when building a default `BrandingMission`.
- `api/state.py` `_MISSION_PLACEHOLDERS` is `("TBD", "To be discussed.", "—", "")` for placeholder detection via `_is_real_value`.

Nothing enforces they stay in sync. Editing one copy without the other can silently diverge default-mission generation from placeholder detection.

## Goals

1. Define the full detection set once in `models.py` (named string constants + composed tuple).
2. Have `_default_mission()` and `api/state.py` both import from that definition.
3. Leave sentinel values, detection behavior, and existing tests unchanged.

## Non-goals

- Changing any placeholder string values.
- Broader `BrandingMission` DTO composition work.
- A new `constants.py` module.
- Coupling `assistant/store` to `api/state` (or the reverse) for these literals.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Placement | `branding_team/models.py` | Both call sites already import domain models from here |
| Shape | Named strings + composed `MISSION_PLACEHOLDERS` tuple | Avoids magic tuple indices in `_default_mission()` |
| Scope of shared set | Full detection tuple including `"—"` and `""` | Single source of truth for all placeholder sentinels |
| Public names | `MISSION_PLACEHOLDER_TBD`, `MISSION_PLACEHOLDER_TO_BE_DISCUSSED`, `MISSION_PLACEHOLDERS` | Clear at import sites; drop private `_MISSION_PLACEHOLDERS` in `state.py` |

## Design

### Constants in `models.py`

Near `BrandingMission` / `BrandingMissionFields`:

```python
MISSION_PLACEHOLDER_TBD = "TBD"
MISSION_PLACEHOLDER_TO_BE_DISCUSSED = "To be discussed."
MISSION_PLACEHOLDERS = (
    MISSION_PLACEHOLDER_TBD,
    MISSION_PLACEHOLDER_TO_BE_DISCUSSED,
    "—",
    "",
)
```

### Call sites

| Site | Change |
|---|---|
| `assistant/store.py` `_default_mission()` | `company_name` / `target_audience` → `MISSION_PLACEHOLDER_TBD`; `company_description` → `MISSION_PLACEHOLDER_TO_BE_DISCUSSED` |
| `api/state.py` | Remove local `_MISSION_PLACEHOLDERS`; import `MISSION_PLACEHOLDERS` and use it in `_is_real_value` |

### Behavior

- `_is_real_value` membership check stays identical (same four strings).
- Default mission fields stay identical string values.
- No runtime API or persistence format changes.

## Testing

Existing coverage should pass unchanged:

- `agents/branding_team/tests/test_conversation_store.py`
- Placeholder / mission detection paths covered via `test_api.py` and related branding API tests

Verify from `backend/`:

```bash
pytest agents/branding_team/tests/test_conversation_store.py agents/branding_team/tests/test_api.py -q
make lint
```

Full `make test` before merge; 90% coverage floor holds (no new uncovered branches expected).

## Success criteria

1. Sentinel string literals for `"TBD"` / `"To be discussed."` used by `_default_mission` appear only as the named constants in `models.py` (composed into `MISSION_PLACEHOLDERS`).
2. `api/state.py` has no local `_MISSION_PLACEHOLDERS` tuple literal.
3. Existing branding conversation-store and placeholder-detection tests pass without assertion rewrites.
4. No unrelated files changed for the fix itself.
