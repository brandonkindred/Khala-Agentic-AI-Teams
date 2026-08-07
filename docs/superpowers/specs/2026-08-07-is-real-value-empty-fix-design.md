# Design: Align `_is_real_value` with its non-empty contract

Date: 2026-08-07

## Goal

Make `_is_real_value` return `True` only when the stripped value is non-empty
and not a known placeholder sentinel, matching its documented postcondition
without relying on `""` being a member of `MISSION_PLACEHOLDERS`.

## Context

Issue #3428 reports that `_is_real_value` in
`backend/agents/branding_team/api/state.py` can treat missing/empty mission
fields as “real” because `(value or "").strip() not in MISSION_PLACEHOLDERS`
is true whenever the empty string is absent from that set.

On current `main`, `MISSION_PLACEHOLDERS` includes `""`, so `None`, `""`, and
whitespace-only inputs already evaluate to `False` and existing unit tests
pass. That is an accidental coupling: emptiness is handled only because empty
is listed as a placeholder. The docstring contract is “non-empty **and** not a
placeholder,” which the implementation does not express.

Chosen approach (option A): fix the predicate explicitly; leave
`MISSION_PLACEHOLDERS` unchanged (including `""`).

## Decisions

| Topic | Choice |
|---|---|
| Approach | Explicit `bool(stripped) and stripped not in MISSION_PLACEHOLDERS` |
| `MISSION_PLACEHOLDERS` | Unchanged (still includes `""`) |
| Callers | No changes; `_mission_has_brand_name` / `_mission_has_minimal_required_fields` keep using `_is_real_value` |
| Scope | One helper + unit tests only |
| Out of scope | Removing `""` from placeholders; broader mission-validation refactors |

## Behavior

### `_is_real_value`

```python
def _is_real_value(value: Optional[str]) -> bool:
    stripped = (value or "").strip()
    return bool(stripped) and stripped not in MISSION_PLACEHOLDERS
```

| Input | Result |
|---|---|
| `None` | `False` |
| `""` | `False` |
| whitespace-only | `False` |
| any `MISSION_PLACEHOLDERS` sentinel | `False` |
| genuine non-empty non-placeholder (e.g. `"Acme Corp"`) | `True` |

### Downstream callers

- `_mission_has_brand_name` — still `_is_real_value(mission.company_name)`
- `_mission_has_minimal_required_fields` — still requires real
  `company_name`, `company_description`, and `target_audience`

Observable behavior for empty/None/whitespace stays the same as today on
`main`; the fix hardens the contract so behavior remains correct even if
`""` is later removed from `MISSION_PLACEHOLDERS`.

## Testing

Extend `backend/agents/branding_team/tests/test_branding_mission_fields.py`:

- Cover `None`, `""`, whitespace-only, each placeholder sentinel, and a real value
- Keep existing assertions that `MISSION_PLACEHOLDERS` still equals
  `(TBD, "To be discussed.", "—", "")`
- Optionally assert `_mission_has_brand_name` /
  `_mission_has_minimal_required_fields` reject empty mission fields

Verification: run the branding mission-field tests; ruff on touched files.

## Files

| File | Change |
|---|---|
| `backend/agents/branding_team/api/state.py` | Update `_is_real_value` body |
| `backend/agents/branding_team/tests/test_branding_mission_fields.py` | Strengthen `_is_real_value` cases |
| `docs/superpowers/specs/2026-08-07-is-real-value-empty-fix-design.md` | This design |

## Acceptance Criteria

- `_is_real_value` returns `False` for `None`, `""`, and whitespace-only strings
- `_is_real_value` returns `False` for known placeholders and `True` only for
  genuine non-empty, non-placeholder values
- Unit tests cover `None`, `""`, whitespace-only, placeholder, and real-value cases
- `_mission_has_brand_name` / `_mission_has_minimal_required_fields` remain correct
- No new lint regressions
- `MISSION_PLACEHOLDERS` contents are unchanged
