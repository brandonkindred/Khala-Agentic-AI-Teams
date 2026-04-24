# PLAN-015: Pantry tracking and subtraction

| Field | Value |
|---|---|
| **Spec** | [SPEC-015](../specs/SPEC-015-nutrition-pantry.md) |
| **Source ADR** | [ADR-005](../adr/ADR-005-nutrition-actionable-workflow-layer.md) |
| **Parent issue** | #220 |
| **Status** | In progress (Phase 0 landing) |
| **Author** | Nutrition & Meal Planning team |
| **Created** | 2026-04-24 |
| **Priority** | P1 |

---

## 1. Scope

Deliver SPEC-015 in four phases per the spec's §5 rollout plan, gated by the
`NUTRITION_PANTRY` feature flag. This plan decomposes SPEC-015 §4.12's 14 work
items into GitHub sub-issues sized for single PRs, identifies the blocker on
SPEC-014, and maps each story to file paths and test files.

## 2. Prerequisites and blockers

| Prereq | Status | Notes |
|---|---|---|
| SPEC-005 (ingredient KB) | ✅ Shipped (v1.0.0, 770 foods) | `backend/agents/nutrition_meal_planning_team/ingredient_kb/` |
| `shared_postgres` Pattern B | ✅ Live for this team | `postgres/__init__.py` exports `SCHEMA`; registered from `api/main.py` lifespan |
| SPEC-014 (grocery list) | ❌ Spec merged, **no code**, no tracking issue | Blocks W4 only |

**SPEC-014 decision:** W4 ("subtract pantry from grocery list") cannot land
until SPEC-014's grocery builder exists. Two options for resolving:

- **(A)** Open a separate SPEC-014 implementation issue and have W4 depend on it.
- **(B)** Fold SPEC-014's minimal grocery-list scaffolding into W4's PR.

Recommend **(A)** — SPEC-014 is a 441-line spec with independent API surface,
rollout, and tests of its own. Treating it as pantry's silent dependency would
bury it.

## 3. Work items

Each item below maps 1:1 to a GitHub sub-issue and targets a single PR.

### W1–W3 + W8: Phase 0 foundation *(shipped in parent PR)*

- **Files:**
  - `backend/agents/nutrition_meal_planning_team/pantry/__init__.py`
  - `backend/agents/nutrition_meal_planning_team/pantry/version.py`
  - `backend/agents/nutrition_meal_planning_team/pantry/types.py`
  - `backend/agents/nutrition_meal_planning_team/pantry/errors.py`
  - `backend/agents/nutrition_meal_planning_team/pantry/store.py`
  - `backend/agents/nutrition_meal_planning_team/pantry/tests/test_pantry_store.py`
  - `backend/agents/nutrition_meal_planning_team/postgres/__init__.py` (+ migration)
  - `backend/agents/nutrition_meal_planning_team/models.py` (+ `pantry_auto_debit`)
- **Acceptance:**
  - `pantry/` module importable; `PANTRY_VERSION = "1.0.0"`.
  - `nutrition_pantry` and `nutrition_pantry_import_drafts` created on lifespan register.
  - `PantryStore` supports add-or-increment, update, delete, get, list (with sort modes), list-expiring.
  - Re-adding an existing `(client_id, canonical_id)` sums `quantity_grams` rather than duplicating (§4.3).
  - `ClientProfile.pantry_auto_debit` defaults to `False`; round-trips through the profile store.
- **Tests:**
  - `test_pantry_store.py` — CRUD round trip, increment semantics, cascade on profile delete, sort modes, `list_expiring` boundaries.

### W4: Grocery-list subtraction *(BLOCKED on SPEC-014)*

- Extends `GroceryItem` with `on_hand_grams`, `needed_grams`, `needed_purchase_qty` (SPEC-015 §4.6, additive).
- `pantry/subtract.py` with `apply(gram_totals, client_id) -> dict[canonical_id, SubtractResult]`.
- Integrates between SPEC-014's aggregation step and its purchase-unit rounding step.
- **Tests:** `test_subtract_exact.py`, `test_subtract_partial.py`, `test_subtract_more.py`, `test_grocery_list_with_pantry.py`.

### W5: Pantry API endpoints

- Routes per SPEC-015 §4.4, mounted on `api/main.py`:
  - `GET/POST/PUT/DELETE /pantry/{client_id}/*`
  - `GET /pantry/{client_id}/expiring?days=N`
- All synchronous, no LLM, gated by `NUTRITION_PANTRY` flag (404 when off).
- **Tests:** `test_api_pantry.py` — status codes, flag gating, increment semantics, validation errors.

### W6: Bulk import parser (two-step confirm)

- `pantry/import_parser.py` — deterministic pass via `ingredient_kb.parse_ingredient`, LLM fallback via `llm_service.generate_structured` returning `ProposedItem[]` + `unresolved`.
- Routes: `POST /pantry/{id}/import`, `POST /pantry/{id}/import/{draft_id}/confirm`, `DELETE /pantry/{id}/import/{draft_id}`.
- Drafts persisted in `nutrition_pantry_import_drafts` with 1-hour TTL.
- **Tests:** `test_import_parser_deterministic.py`, `test_import_parser_llm_fallback.py`, `test_import_confirm_subset.py`, `test_import_roundtrip.py`.

### W7: Near-expiry hints in `/plan/meals`

- `pantry/expiry.py` — `list_expiring(client_id, days=N) -> list[PantryItem]` and a `format_prompt_hint` helper (§4.7).
- Orchestrator hook in `orchestrator/agent.py` inserts hints into the planner prompt when pantry is non-empty.
- **Tests:** `test_plan_hints_from_pantry.py`, `test_expiring_query.py` (threshold edges).

### W8: `pantry_auto_debit` profile flag *(shipped in parent PR)*

Profile field only; the hook consumer lands in SPEC-018 cook-mode.

### W9–W12: Frontend UI *(Angular)*

- **W9** pantry tab list view + empty state
- **W10** add/edit/remove modals
- **W11** import preview modal
- **W12** grocery-list "on hand" chips (lands alongside W4)

### W13: Observability

- Counters from SPEC-015 §4.10: `pantry.item_{added,removed}{source}`, `pantry.expiring_items_hinted`, `pantry.import.draft_{created,confirmed}`, `pantry.import.unresolved_lines`, `pantry.subtract.applied`.

### W14: Benchmarks

- `pantry.read ≤ 30 ms`, `pantry.subtract ≤ 10 ms` per SPEC-015 §4.14.

## 4. Rollout

Mirrors SPEC-015 §5 exactly:

- **Phase 0 — Foundation (P0):** W1–W3 + W8. *This PR.* Migration in staging; no runtime surface.
- **Phase 1 — Core pantry behind flag (P0):** W4, W5. Dogfood on internal users; grocery-list `on_hand` fields appear with flag on.
- **Phase 2 — Hints + import (P1):** W6, W7, W9–W12. Reviewer gate: hints in 4/5 plans with expiring items; import preview correct on 8/10 dumps.
- **Phase 3 — Ramp (P1):** 10% → 50% → 100% over two weeks. Watch pantry growth curve, import usage, on-hand rate.
- **Phase 4 — Cleanup (P1/P2):** W11 polish, W13, W14. Flag default-on; removal scheduled.

Rollback = flag off. Endpoints 404, grocery-list omits `on_hand` fields (backwards-compatible). Pantry rows retained; migration is additive.

## 5. Test inventory (cumulative across all work items)

Copied from SPEC-015 §6. Each test file lands with the work item that ships it.

**Unit:** `test_pantry_crud.py` (W3, landing as `test_pantry_store.py`), `test_subtract_exact.py` / `test_subtract_partial.py` / `test_subtract_more.py` (W4), `test_import_parser_deterministic.py` / `test_import_parser_llm_fallback.py` / `test_import_confirm_subset.py` (W6), `test_expiring_query.py` (W7).

**Integration:** `test_grocery_list_with_pantry.py` (W4), `test_plan_hints_from_pantry.py` (W7), `test_import_roundtrip.py` (W6), `test_auto_debit_hook_placeholder.py` (W8; sanity test for SPEC-018 surface).

## 6. Observability

All counters from SPEC-015 §4.10 emit via the existing team logger. W13 audits that every counter fires at least once in staging before Phase 4.

## 7. Sub-issue tracker

| Work item | Issue | PR | Status |
|---|---|---|---|
| W1–W3 + W8 foundation | *(this PR's issue)* | *(this PR)* | In review |
| W4 grocery subtraction | TBD | — | Blocked on SPEC-014 |
| W5 API endpoints | TBD | — | Open |
| W6 bulk-import parser | TBD | — | Open |
| W7 near-expiry hints | TBD | — | Open |
| W9 pantry tab | TBD | — | Open |
| W10 add/edit modals | TBD | — | Open |
| W11 import preview modal | TBD | — | Open |
| W12 on-hand chips | TBD | — | Open (paired with W4) |
| W13 observability | TBD | — | Open |
| W14 benchmarks | TBD | — | Open |

Filled in as sub-issues land. Parent: #220.
