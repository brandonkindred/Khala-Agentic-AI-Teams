# SPEC-015: Pantry tracking and subtraction

| Field       | Value                                                    |
|-------------|----------------------------------------------------------|
| **Status**  | Proposed                                                 |
| **Author**  | Nutrition & Meal Planning team                           |
| **Created** | 2026-04-17                                               |
| **Priority**| P1 within ADR-005 (enhances SPEC-014; unblocks pantry-aware planning hints) |
| **Scope**   | New module `backend/agents/nutrition_meal_planning_team/pantry/`, Postgres tables, API endpoints, UI pantry screen, bulk-import LLM parse, optional auto-debit in cook mode |
| **Depends on** | SPEC-005 (canonical ids, densities, purchase units), SPEC-014 (grocery list) |
| **Implements** | ADR-005 §2 (pantry) |

---

## 1. Problem Statement

The grocery list from SPEC-014 tells users what to buy for their
plan. It does not yet account for what they already have. A
realistic pantry model is where most meal planners bounce users —
the data-entry cost is high and the payoff is invisible if we just
use it as a filter.

This spec ships pantry tracking and pantry-aware grocery
generation with three explicit hedges against data-entry fatigue:

1. The pantry is fully usable empty — it is always a filter, never
   a requirement.
2. Bulk import via text dump + LLM parse, surfaced to the user for
   confirmation before anything is written.
3. Near-expiry hints that surface on plan generation — making the
   pantry *useful* (reducing waste, informing planning) rather than
   just *maintained*.

Optional auto-debit of pantry quantities on cook events lands in
SPEC-018 (cook mode); this spec defines the hook.

---

## 2. Current State

### 2.1 Today

- Grocery list aggregates ingredients for a plan but subtracts
  nothing (SPEC-014 §3.2 explicit non-goal).
- No pantry data structure, table, or endpoint.
- No hint signal to the planner about near-expiry items.

### 2.2 Gaps

1. Users buy what they already have; trust in the list erodes.
2. Food waste from forgotten items is not addressable.
3. Planning cannot opportunistically use what is already home.

---

## 3. Goals and Non-Goals

### 3.1 Goals

- Ship pantry data model and endpoints (`GET/POST/PUT/DELETE`) keyed
  by `(client_id, canonical_id)`.
- Integrate with SPEC-014's grocery list: subtract pantry
  quantities before rounding; report both "needed" and "on hand"
  per item.
- Bulk import from a text dump via `POST /pantry/import` with
  LLM parse + user confirmation step.
- Near-expiry hints plumbed into `POST /plan/meals`: items
  expiring within N days are passed to the meal-planning prompt
  as soft preferences.
- Optional auto-debit on cook events (SPEC-018) — the data path
  exists in this spec as an opt-in flag on the profile.
- Pantry UI that is usable empty and scales gracefully to dozens
  of items.

### 3.2 Non-goals

- **No barcode scanning.** Image / barcode ingestion is a mobile-
  app feature, out of scope for v1.
- **No automatic expiry dating.** Users enter `expires_on` when
  they know it; we do not guess.
- **No multi-household pantries.** One pantry per client. Shared
  households can be addressed later.
- **No pantry-first meal recommendation agent.** "What can I make
  with what I have?" is a distinct feature, backlogged.
- **No unit of measure edit per pantry row in v1.** We store in
  grams plus a user-facing display unit; editing the display unit
  is v1.1.

---

## 4. Detailed Design

### 4.1 Module layout

```
backend/agents/nutrition_meal_planning_team/pantry/
├── __init__.py              # pantry store + grocery subtractor
├── version.py               # PANTRY_VERSION = "1.0.0"
├── types.py                 # PantryItem, PantryImportDraft
├── store.py                 # Postgres-backed CRUD
├── subtract.py              # grocery-list subtraction logic
├── import_parser.py         # LLM-backed bulk parse; structured output
├── expiry.py                # near-expiry query + hint formatter
├── errors.py
└── tests/
```

### 4.2 Types

```python
@dataclass(frozen=True)
class PantryItem:
    client_id: str
    canonical_id: str
    quantity_grams: float         # canonical storage unit
    display_qty: float            # what the user entered
    display_unit: str             # 'count', 'ml', 'g', 'package', etc.
    expires_on: Optional[date] = None
    notes: str = ""
    added_at: str
    updated_at: str

@dataclass(frozen=True)
class PantryImportDraft:
    id: str                       # short-lived draft id
    client_id: str
    proposed: tuple[ProposedItem, ...]
    unresolved: tuple[str, ...]
    created_at: str

@dataclass(frozen=True)
class ProposedItem:
    raw_line: str
    canonical_id: Optional[str]
    display_name: str
    display_qty: Optional[float]
    display_unit: Optional[str]
    quantity_grams: Optional[float]
    confidence: float
```

### 4.3 Postgres

Migration `011_pantry.sql`:

```sql
CREATE TABLE IF NOT EXISTS nutrition_pantry (
    client_id         TEXT NOT NULL REFERENCES nutrition_profiles(client_id)
                          ON DELETE CASCADE,
    canonical_id      TEXT NOT NULL,
    quantity_grams    DOUBLE PRECISION NOT NULL,
    display_qty       DOUBLE PRECISION,
    display_unit      TEXT,
    expires_on        DATE,
    notes             TEXT,
    added_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, canonical_id)
);
CREATE INDEX ON nutrition_pantry (client_id, expires_on) WHERE expires_on IS NOT NULL;

CREATE TABLE IF NOT EXISTS nutrition_pantry_import_drafts (
    draft_id     TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,  -- short TTL, e.g. 1 hour
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

One row per `(client_id, canonical_id)`. Re-adding an existing
canonical id on POST increments the existing quantity rather than
inserting a duplicate.

### 4.4 API

| Method | Path | Purpose |
|--------|------|---------|
| `GET`    | `/pantry/{client_id}` | List pantry items; optional `?sort=expiring\|name\|added_desc` |
| `POST`   | `/pantry/{client_id}/items` | Add one item; body: `{canonical_id OR raw_name, display_qty, display_unit, expires_on?, notes?}` |
| `PUT`    | `/pantry/{client_id}/items/{canonical_id}` | Update quantity/expiry |
| `DELETE` | `/pantry/{client_id}/items/{canonical_id}` | Remove |
| `POST`   | `/pantry/{client_id}/import` | Submit a text dump; returns `PantryImportDraft` for user review |
| `POST`   | `/pantry/{client_id}/import/{draft_id}/confirm` | Commit a reviewed draft (accepting subset of proposed items) |
| `DELETE` | `/pantry/{client_id}/import/{draft_id}` | Discard draft |
| `GET`    | `/pantry/{client_id}/expiring?days=3` | Items expiring within `days` (default 3) |

All write endpoints are synchronous. Import parse is the only
endpoint that calls an LLM; it is explicitly a two-step (parse →
confirm) flow.

### 4.5 Bulk import parser

`POST /pantry/{client_id}/import` accepts a text blob:

```
2 onions
half a bag of frozen spinach
1 L milk, use by Apr 20
mysterious leftovers (some tomato sauce)
```

Flow:

1. Split on newlines and commas.
2. For each line, first run SPEC-005's `parse_ingredient` to
   resolve deterministically.
3. For lines that fail to resolve confidently, call
   `llm_service.structured_output` with a locked schema returning
   `ProposedItem[]`. Prompt rules:
   - *"Only propose items that map to the provided canonical food
     list."*
   - *"If a line does not map, put it in `unresolved`, do not
     invent."*
   - *"Quantities that are not numeric (e.g. 'some', 'half a bag')
     return `None` and we ask the user."*
4. Build a `PantryImportDraft`, persist it for the confirm step.
5. Return the draft to the user for review.

The confirm step takes `{accept: [canonical_id], modify: [ProposedItem]}`
and commits. Unaccepted proposals are dropped. Unresolved raw
lines are never silently added.

### 4.6 Grocery-list subtraction (integration with SPEC-014)

SPEC-014's `GroceryList` grows two fields per item (additive):

```python
class GroceryItem(BaseModel):
    # existing...
    on_hand_grams: float = 0.0
    needed_grams: float = 0.0      # total_grams - on_hand_grams
    needed_purchase_qty: float = 0.0  # rounded from needed_grams
```

`build_grocery_list` is updated to call
`pantry.subtract.apply(gram_totals, client_id)` before purchase-
unit conversion. Ordering:

1. Aggregate by canonical_id (existing SPEC-014 step).
2. Subtract pantry quantities (new).
3. Apply waste buffer and purchase-unit rounding (existing).
4. Expose both `total_grams` and `on_hand_grams` in the response
   so the UI can show "needed 300 g, have 100 g" clearly.

Edge cases:

- Pantry has more than the recipe needs → `needed_grams=0`; the
  item does not appear in the shopping total but remains visible
  with a "✓ on hand" chip.
- Pantry items not in the plan are untouched.
- Regeneration on plan change re-subtracts; no persistent pantry
  state leaks across plans.

### 4.7 Near-expiry hints

`GET /pantry/{client_id}/expiring?days=3` returns items with
`expires_on - now ≤ days`.

On `POST /plan/meals` (SPEC-010 orchestrator), before the planner
runs, fetch expiring items and include them in the prompt as a
soft preference:

```
Pantry items expiring soon (prefer recipes that use them):
  - Tofu (firm, 400 g, expires in 2 days)
  - Cilantro (small bunch, expires in 3 days)
  - Greek yogurt (200 g, expires in 4 days)
```

The planner does **not** have to use them. Restrictions and targets
still win. Hints are one-shot per plan; we do not re-assert them on
swap unless the user re-plans.

### 4.8 Auto-debit hook (for SPEC-018)

Profile additive field:

```python
class ClientProfile(BaseModel):
    # existing...
    pantry_auto_debit: bool = False
```

Off by default. When on, SPEC-018's `POST /cooked` event reads
the recipe's parsed ingredients and decrements
`nutrition_pantry.quantity_grams` by the proportional amount. If
decrement would go negative, clamp to 0 and log a structured
event (audit; mismatched pantry happens and is fine — we surface
it, we do not try to reason about it).

This spec only defines the hook and the profile flag. The actual
hook consumer is SPEC-018.

### 4.9 UI

- Pantry tab on the profile or plan screen.
- Empty state: large "add items" affordance + "import from a text
  dump" CTA. Deliberately inviting — low activation cost.
- List view sorted by expiry first, then alphabetical.
- Each row shows display qty/unit, expiry pill (colored by
  proximity), notes, and quick actions (edit, remove, -1 usage,
  mark finished).
- Import flow: paste → preview modal with per-line checkboxes →
  confirm.
- On the grocery list (SPEC-014), "on hand" chips visible per
  item; clicking drills through to the pantry row.
- Plan view: banner "3 items are expiring soon — we mentioned them
  when generating your plan" (when hints influenced the prompt).

### 4.10 Observability

- `pantry.item_added{source}` — `manual | import | grocery_check_in`.
- `pantry.item_removed{source}` — `manual | auto_debit | cleanup`.
- `pantry.expiring_items_hinted` per plan generation.
- `pantry.import.draft_created` and `pantry.import.draft_confirmed`.
- `pantry.import.unresolved_lines` histogram.
- `pantry.subtract.applied` per grocery-list build.

### 4.11 Privacy

- Pantry is personal. Deleted on account deletion via cascade.
- Import parse LLM calls do not include client_id or profile
  context; only the pasted text + the canonical taxonomy.
- Raw text dumps are not logged at INFO or above; only length and
  resolution counts.

### 4.12 Priority-grouped work items

| # | Item | Priority |
|---|------|----------|
| W1 | Module scaffolding, version, types | P0 |
| W2 | Migration `011_pantry.sql` + schema registration | P0 |
| W3 | `store.py` CRUD + tests | P0 |
| W4 | `subtract.py` with integration into SPEC-014 grocery builder | P0 |
| W5 | API endpoints (single-item CRUD + GET expiring) | P0 |
| W6 | Import parser with two-step (parse → confirm) flow | P1 |
| W7 | Near-expiry hints plumbed into SPEC-010 planner call | P1 |
| W8 | `pantry_auto_debit` flag on profile (additive) | P0 |
| W9 | UI: pantry tab list view + empty state | FE | P1 |
| W10 | UI: add/edit/remove modals | FE | P1 |
| W11 | UI: import preview modal | FE | P2 |
| W12 | UI: grocery list "on hand" chips | FE | P1 |
| W13 | Observability counters | P1 |
| W14 | Benchmarks: pantry read ≤ 30 ms; subtract ≤ 10 ms | P2 |

---

## 5. Rollout Plan

Flag `NUTRITION_PANTRY` (off → endpoints hidden, on → surfaced).

### Phase 0 — Foundation (P0)
- [ ] W1–W3 landed. Migration in staging.

### Phase 1 — Core pantry behind flag (P0)
- [ ] W4–W5, W8 landed.
- [ ] SPEC-014 grocery-list `on_hand` fields appear when flag on.
- [ ] Flag on internal. Dogfood team adds items and shops from
      reduced list.

### Phase 2 — Hints + import (P1)
- [ ] W6, W7 landed.
- [ ] W9–W12 UI shipped.
- [ ] Acceptance gate: reviewer agrees that near-expiry hints
      show up in 4 of 5 plans generated with expiring items in
      the pantry; import preview is correct on 8 of 10 dumps.

### Phase 3 — Ramp (P1)
- [ ] 10% → 50% → 100% over two weeks.
- [ ] Watch: pantry item count per user (growth curve), import
      usage, grocery-list "✓ on hand" rate.

### Phase 4 — Cleanup (P1/P2)
- [ ] W11 import modal polish; W13 observability; W14 benchmarks.
- [ ] Flag default on; removal scheduled.

### Rollback
- Flag off → endpoints return 404; grocery-list omits `on_hand`
  fields (backwards-compatible JSON for the UI).
- Pantry rows retained.
- Additive migration.

---

## 6. Verification

### 6.1 Unit tests

- `test_pantry_crud.py` — round-trip CRUD; adding existing
  canonical id increments rather than duplicates.
- `test_subtract_exact.py` — pantry has exactly what the plan
  needs → needed_grams = 0.
- `test_subtract_partial.py` — pantry has less → needed_grams =
  total - on_hand.
- `test_subtract_more.py` — pantry has more → needed_grams = 0,
  extra is not consumed.
- `test_import_parser_deterministic.py` — lines resolvable by
  SPEC-005 do not call LLM.
- `test_import_parser_llm_fallback.py` — ambiguous lines produce
  ProposedItem with confidence scores and reach the preview.
- `test_import_confirm_subset.py` — confirming 2 of 3 proposed
  items commits only those 2.
- `test_expiring_query.py` — boundary cases at the `days`
  threshold.

### 6.2 Integration tests

- `test_grocery_list_with_pantry.py` — plan plus pantry produces
  correct `needed` vs. `total` fields; items fully covered by
  pantry are marked on-hand; removing a pantry item regenerates
  correctly.
- `test_plan_hints_from_pantry.py` — pantry with an item expiring
  in 2 days causes the planner prompt to include that item; with
  no expiring items the prompt is unchanged.
- `test_import_roundtrip.py` — paste → draft → confirm (subset) →
  pantry reflects confirmed items only.
- `test_auto_debit_hook_placeholder.py` — hook interface callable;
  no behavior without SPEC-018 wiring (sanity test for future
  integration).

### 6.3 Reviewer audit (Phase 2)

- 5 internal users paste their actual pantries for import; review
  preview accuracy. Target: ≥80% of lines resolved to a canonical
  id with correct qty/unit.
- 5 plans generated with 2–5 expiring items in the pantry; review
  whether the planner incorporated them where sensible.

### 6.4 Observability

All §4.10 counters emit in staging.

### 6.5 Privacy

Log-redaction grep: zero pantry item names or import text at INFO
or above in production logs.

### 6.6 Cutover criteria

- All P0/P1 tests green.
- Phase 2 reviewer gates met.
- Phase 3 ramp: import-resolution rate ≥ 80%, no data-loss
  incidents.

---

## 7. Open Questions

- **Quantity semantics: bags, packages.** "Half a bag of frozen
  spinach" — we do not reliably know a bag's grams. v1 surfaces
  this as unresolved-quantity and asks the user. Later we can seed
  package-size metadata on `canonical_foods.yaml`.
- **Leftover tracking.** Cooking 4 servings when the user eats 2
  leaves 2 servings of leftovers. v1 has no leftover model;
  SPEC-018's cook-mode event could opt-in to "leftovers saved"
  which becomes a pantry add. Backlogged.
- **Mobile-first input.** v1 is web. Adding items one by one is
  clunky. Barcode scanning (mobile) and photo OCR are on the v2
  roadmap.
- **Multi-profile household.** Out of scope here; also
  pre-empted in ADR-001 household-member work which is profile-
  shared, not pantry-shared.
