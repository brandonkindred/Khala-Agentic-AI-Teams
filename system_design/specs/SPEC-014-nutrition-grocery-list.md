# SPEC-014: Consolidated grocery list

| Field       | Value                                                    |
|-------------|----------------------------------------------------------|
| **Status**  | Proposed                                                 |
| **Author**  | Nutrition & Meal Planning team                           |
| **Created** | 2026-04-17                                               |
| **Priority**| P0 within ADR-005 (blocks SPEC-015 pantry subtraction)   |
| **Scope**   | New module `backend/agents/nutrition_meal_planning_team/grocery/`, Postgres table, API endpoints, UI grocery view |
| **Depends on** | SPEC-005 (parser, densities, purchase units), SPEC-007 (parsed ingredients on recorded meals) |
| **Implements** | ADR-005 §1 (consolidated grocery list) |

---

## 1. Problem Statement

Users are handed seven recipes and then face the most friction-heavy
step in the whole workflow: reconciling overlapping ingredients,
converting between the recipe's units and what the store sells, and
building a list grouped by section. Most meal-planning tools lose
users here.

This spec ships the consolidated grocery list: aggregate the
canonicalized ingredients across a plan, convert to
purchasable units, round with a waste buffer, and group by
supermarket aisle. It is the first ADR-005 capability because it
unlocks pantry subtraction (SPEC-015) and substitution (SPEC-016),
which both operate on the same canonicalized quantities.

---

## 2. Current State

### 2.1 Today's flow

After SPEC-007 + SPEC-009 + SPEC-010 land:
- Every recorded meal has parsed ingredients with canonical ids,
  quantities, and units (SPEC-007 `parsed_ingredients_json`).
- SPEC-009's density table maps unit-based quantities to grams.
- There is no user-visible aggregation. No grocery list endpoint,
  no UI, no Postgres row.

### 2.2 Gaps

1. No aggregation across recipes in a plan.
2. No purchase-unit conversion — "1 tbsp olive oil" × 7 = 105 ml =
   one bottle, but we do not compute the last step.
3. No aisle grouping.
4. No persistence — a user who reloads the page loses the list.
5. No export path.

---

## 3. Goals and Non-Goals

### 3.1 Goals

- Ship `POST /plan/meals/{plan_id}/grocery-list` that returns a
  structured `GroceryList`, persists it, and supports regeneration.
- Aggregate canonical ingredients across a plan in grams (via
  SPEC-005 densities) and convert to **purchase units** using a
  new `purchase_unit` field on `canonical_foods.yaml`.
- Round up with a configurable waste buffer (default 10%) and
  group by aisle tag.
- Support export formats: plain text, markdown, CSV, signed JSON.
- Persist in Postgres keyed by plan id; idempotent regeneration.
- Zero LLM calls on the critical path — the list is pure
  arithmetic over SPEC-005 data.

### 3.2 Non-goals

- **No pantry subtraction.** SPEC-015.
- **No third-party fulfillment.** Instacart / Amazon Fresh adapters
  ride on the exported JSON shape in a future spec.
- **No UI cart persistence across plans.** One list per plan; a
  new plan gets its own list.
- **No quantity editing by the user within the list in v1.** The
  UI shows the computed list; users can override by editing the
  plan's recipes or via a manual add. Fine-grained item-level
  editing is v1.1.
- **No recipe scaling.** A day's recipes are aggregated as-planned.
  Per-day servings adjustments are out of scope.

---

## 4. Detailed Design

### 4.1 Module layout

```
backend/agents/nutrition_meal_planning_team/grocery/
├── __init__.py              # build_grocery_list, GROCERY_VERSION
├── version.py               # GROCERY_VERSION = "1.0.0"
├── types.py                 # GroceryList, GroceryItem, AisleGroup
├── aggregate.py             # sum by canonical_id in grams
├── purchase_units.py        # grams -> purchase_unit conversion + rounding
├── aisle_group.py           # canonical_id -> aisle_tag mapping + ordering
├── export.py                # text, markdown, csv, json exporters
├── errors.py
└── tests/
```

### 4.2 Data additions

`canonical_foods.yaml` (SPEC-005) gains two fields per food:

```yaml
- id: olive_oil
  display_name: "Olive oil"
  purchase_unit:
    unit: ml
    typical_package_ml: 500       # smallest typical package
    pack_sizes_ml: [250, 500, 750, 1000]   # optional, for smart rounding
  aisle_tag: pantry
```

`aisle_tag` enum (closed):
`produce | meat_fish | dairy_eggs | pantry | spices | frozen |
bakery | beverages | condiments | other`.

Adding or changing `purchase_unit` requires a minor KB_VERSION
bump per SPEC-005 §4.10. Reviewer sign-off as usual.

### 4.3 Types

```python
@dataclass(frozen=True)
class GroceryItem:
    canonical_id: str
    display_name: str
    aisle_tag: AisleTag
    total_grams: float                       # aggregated mass
    purchase_unit: str                       # 'ml', 'g', 'count'
    purchase_qty: float                      # rounded, ready-to-buy
    purchase_qty_raw: float                  # pre-rounding, for audit
    contributing_recipe_ids: tuple[str, ...] # recipes that asked for it
    buffer_applied_pct: float
    confidence: float                        # inherits from SPEC-009 parse

@dataclass(frozen=True)
class AisleGroup:
    aisle_tag: AisleTag
    items: tuple[GroceryItem, ...]

@dataclass(frozen=True)
class GroceryList:
    plan_id: str
    client_id: str
    groups: tuple[AisleGroup, ...]           # fixed aisle order
    unresolved: tuple[str, ...]              # recipe ingredients that didn't resolve
    low_confidence: tuple[str, ...]          # items with parse confidence < 0.85
    waste_buffer_pct: float
    grocery_version: str
    kb_version: str
    generated_at: str
```

### 4.4 Aggregation

`aggregate.py::aggregate_plan(plan: MealPlanResponse) -> dict[str, float]`:

- Iterate every suggestion's `parsed_ingredients_json` from
  SPEC-007.
- For each parsed ingredient with a known `canonical_id`, use
  SPEC-005's `convert_to_grams` to get grams.
- Skip ingredients with `canonical_id=None` — those go to
  `unresolved` on the output (user-visible).
- Skip `qty=None` items that have no default-quantity entry;
  they go to `low_confidence`.
- Sum by `canonical_id`.

Returns the grams map, plus the `unresolved` and `low_confidence`
lists.

### 4.5 Purchase-unit conversion

`purchase_units.py::to_purchase_unit(canonical_id, grams) -> PurchaseQty`:

- Lookup `purchase_unit` on the canonical food.
- Convert grams to the purchase unit using the inverse density
  (for volume-purchase items like olive oil) or the item's mass-
  per-count (for count-purchase items like eggs, onions).
- Apply `buffer_pct` (default 10%) to the raw quantity.
- Round to the nearest sensible increment:
  - `ml`/`g`: round up to nearest 50 unless `pack_sizes_*` is set,
    then pick the smallest pack size that covers.
  - `count`: integer ceiling (2.1 onions → 3 onions).
- Report `purchase_qty` and `purchase_qty_raw` so the user can see
  how we rounded.

Spices and small pantry items with trivial contributions (< 5 g
across the whole plan) are still surfaced, but with a note
"probably already in your pantry" — the UI can de-emphasize them.

### 4.6 Aisle grouping and ordering

- `aisle_tag` on each canonical food.
- Fixed group order in the list:
  `produce → meat_fish → dairy_eggs → bakery → pantry → spices →
  frozen → condiments → beverages → other`.
- Within a group, items sorted alphabetically by `display_name`.
- Groups with zero items are omitted.

### 4.7 API

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/plan/meals/{plan_id}/grocery-list` | Build or rebuild the list; persists latest. Body optional: `{waste_buffer_pct?: float}` |
| `GET`  | `/plan/meals/{plan_id}/grocery-list` | Fetch latest persisted list |
| `GET`  | `/plan/meals/{plan_id}/grocery-list/export?format=text\|markdown\|csv\|json` | Export in the requested format |
| `POST` | `/plan/meals/{plan_id}/grocery-list/items/manual` | Add a manual item (free text + optional canonical_id) |
| `DELETE` | `/plan/meals/{plan_id}/grocery-list/items/{canonical_id}` | Remove a computed item (user knows they have it) |

The manual-item endpoint accepts a canonical_id OR a raw string;
raw strings are parsed at save time via SPEC-005 and stored with
a `source="manual"` tag so the UI can render them distinctly.

### 4.8 Persistence

Migration `010_grocery_lists.sql`:

```sql
CREATE TABLE IF NOT EXISTS nutrition_grocery_lists (
    plan_id               TEXT PRIMARY KEY,
    client_id             TEXT NOT NULL,
    list_json             JSONB NOT NULL,
    waste_buffer_pct      REAL NOT NULL DEFAULT 10.0,
    grocery_version       TEXT NOT NULL,
    kb_version            TEXT NOT NULL,
    generated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON nutrition_grocery_lists (client_id, generated_at DESC);

CREATE TABLE IF NOT EXISTS nutrition_grocery_list_manual_items (
    id               BIGSERIAL PRIMARY KEY,
    plan_id          TEXT NOT NULL REFERENCES nutrition_grocery_lists(plan_id)
                         ON DELETE CASCADE,
    canonical_id     TEXT,
    raw              TEXT,
    purchase_qty     DOUBLE PRECISION,
    purchase_unit    TEXT,
    added_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nutrition_grocery_list_removed_items (
    plan_id          TEXT NOT NULL REFERENCES nutrition_grocery_lists(plan_id)
                         ON DELETE CASCADE,
    canonical_id     TEXT NOT NULL,
    removed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, canonical_id)
);
```

On regeneration (e.g., after a `/swap` or plan edit), the list is
recomputed but manual-add items and user-removed items are
preserved (the removed set is applied as a filter after
aggregation; manual adds are concatenated at the end of their
aisle).

### 4.9 Exports

- **Text**: plain markdown-less format, one item per line grouped
  by `## AISLE` headers. Copy-paste friendly for users who shop
  with a notes app.
- **Markdown**: same but with `- [ ] ` checkboxes.
- **CSV**: `aisle_tag,canonical_id,display_name,purchase_qty,purchase_unit`.
- **JSON**: serialized `GroceryList`.

Exports are signed with a short-lived HMAC in a `grocery-list-token`
query parameter so the same URL can be shared with a partner who
has no account (read-only), without creating a full share/auth
surface.

### 4.10 Regeneration triggers

The list auto-regenerates on:

- Any `POST /swap` (SPEC-010) that replaces a recipe in the plan.
- Any plan-level edit that adds/removes a recipe.
- Explicit `POST /grocery-list` with `force=true`.

Auto-regeneration preserves manual adds and removals. A regen
diff is returned to the UI so it can show "Added 2, removed 1"
toast notifications.

### 4.11 UI

- Grocery tab on the plan screen.
- Aisle accordions collapsible; each item a checkbox.
- "Needed vs. pantry" split shown as a banner (SPEC-015 populates;
  until SPEC-015 lands the banner is absent).
- Manual add field with canonical-id autocomplete.
- Export menu with the four formats.
- A small "how we rounded" tooltip on each item shows
  `purchase_qty_raw → purchase_qty` and the waste buffer.

### 4.12 Performance

Synchronous endpoint budget:
- Aggregation: O(recipes × ingredients) — typically <200 items;
  target ≤ 30 ms.
- Purchase-unit conversion: O(unique canonical_ids), typically <60;
  target ≤ 10 ms.
- Total: p99 ≤ 100 ms.

### 4.13 Priority-grouped work items

| # | Item | Priority |
|---|------|----------|
| W1 | Module scaffolding, version, types | P0 |
| W2 | `canonical_foods.yaml` extension: `purchase_unit`, `aisle_tag`; SPEC-005 KB version bump | P0 |
| W3 | Migration `010_grocery_lists.sql` + schema registration | P0 |
| W4 | `aggregate.py` + tests (multi-recipe aggregation) | P0 |
| W5 | `purchase_units.py` + rounding + pack-size tests | P0 |
| W6 | `aisle_group.py` + ordering tests | P0 |
| W7 | `POST/GET /grocery-list` + persistence | P0 |
| W8 | Manual add + removal endpoints and persistence | P1 |
| W9 | `export.py` all four formats + signed URL middleware | P1 |
| W10 | Auto-regeneration on plan/swap events | P1 |
| W11 | UI: grocery tab with aisle accordions + checkboxes | FE | P1 |
| W12 | UI: manual add + autocomplete | FE | P1 |
| W13 | UI: "how we rounded" tooltip + regen diff toast | FE | P2 |
| W14 | Observability counters | P1 |
| W15 | Benchmarks: p99 ≤ 100 ms | P2 |

---

## 5. Rollout Plan

Flag `NUTRITION_GROCERY_LIST` (off → no endpoints exposed, on →
endpoints live).

### Phase 0 — KB extension (P0)
- [ ] W2 KB fields added across the 2,000 seed foods. Migration
      applied in staging.
- [ ] SPEC-005 KB_VERSION minor bump; SPEC-007/SPEC-009 caches
      invalidated.

### Phase 1 — Backend behind flag (P0)
- [ ] W1, W3–W7 landed behind flag.
- [ ] Flag on internal. Dogfood-team profiles get lists on their
      existing plans.

### Phase 2 — UI + exports (P1)
- [ ] W8–W11 landed.
- [ ] Internal dogfood: generate + shop from a list. Review
      rounding, aisle groupings, and edge cases.
- [ ] Acceptance gate: reviewer agrees aggregation is correct on
      10 planned dogfood shops; pack-size rounding feels
      reasonable.

### Phase 3 — Ramp (P1)
- [ ] 10% → 50% → 100% over two weeks.
- [ ] Watch: manual-add rate (indicator of KB gaps), removal rate
      (indicator of over-buying), export usage.

### Phase 4 — Cleanup (P1/P2)
- [ ] W12–W15 landed.
- [ ] Flag default on; removal scheduled.

### Rollback
- Flag off → endpoints return 404; existing persisted lists remain
  but are not surfaced in UI.
- Additive migration.

---

## 6. Verification

### 6.1 Unit tests

- `test_aggregate_multi_recipe.py` — 3 recipes each calling for 1
  tbsp olive oil → 45 ml aggregate; 1 cup rice across 2 recipes →
  aggregated in grams via density table.
- `test_purchase_unit_ml.py` — 180 ml of olive oil with 10% buffer
  rounds up to 250 ml pack size.
- `test_purchase_unit_count.py` — 2.1 onions → 3 onions.
- `test_aisle_ordering.py` — produce before pantry before spices;
  empty aisles omitted.
- `test_unresolved_and_low_confidence.py` — unresolved ingredients
  surface in the dedicated list, not dropped silently.

### 6.2 Integration tests

- `test_grocery_list_endpoint.py` — `POST` returns a list; `GET`
  returns the same; regeneration with a new recipe updates.
- `test_swap_triggers_regen.py` — after SPEC-010 `/swap`, the
  persisted grocery list is updated; diff returned.
- `test_manual_add_preserved.py` — manual add survives
  regeneration; regenerating twice does not duplicate manual items.
- `test_removed_item_respected.py` — user removes garlic; regen
  keeps garlic absent; adding a new recipe that uses garlic does
  NOT resurrect it (the removal persists until user un-removes).
- `test_export_formats.py` — all four formats produce valid
  output; signed JSON URL verifies.

### 6.3 Fixture-based correctness

`tests/fixtures/plans/` from SPEC-009 reused: for each, a
hand-computed expected grocery list exists; aggregation must
match within unit-rounding tolerance.

### 6.4 Performance

- `bench_grocery_list.py` — 7-day plan with 14 recipes, 100 unique
  canonical ingredients: p99 ≤ 100 ms on CI reference runner.

### 6.5 Observability

Counters:
- `grocery.built{outcome}`
- `grocery.unresolved_ingredient_count` histogram
- `grocery.manual_add{has_canonical_id}`
- `grocery.removed_item`
- `grocery.export{format}`

### 6.6 Cutover criteria

- All P0/P1 tests green.
- Phase 2 dogfood acceptance gate met.
- Phase 3 ramp: unresolved rate on new plans < 5%; rounding
  complaints < 2 per 100 users.
- Clinical / nutrition team lead sign-off on aisle taxonomy.

---

## 7. Open Questions

- **Pack-size data availability.** `pack_sizes_ml`/`pack_sizes_g`
  is optional per canonical food. v1 seeds the top-200 most-
  bought items; the long tail falls back to coarse 50 ml / 50 g
  rounding. Acceptable compromise; backlogged refinement.
- **Recipe scaling.** If a user scales a recipe ("double
  Saturday's dinner — guests"), the grocery list needs to
  scale too. v1 does not support recipe-level scaling; v1.1.
- **Unit preferences.** US users may prefer imperial in exports
  (tbsp, oz, cups). v1 exports in the same purchase units we
  computed (mostly ml/g/count). Unit-toggle UX is v1.1.
- **Cross-plan merging.** Some users plan two weeks at a time.
  v1 is one list per plan; a "merge two plans' lists" feature
  is out of scope.
