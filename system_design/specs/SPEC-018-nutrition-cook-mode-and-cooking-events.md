# SPEC-018: Cook mode and structured cooking events

| Field       | Value                                                    |
|-------------|----------------------------------------------------------|
| **Status**  | Proposed                                                 |
| **Author**  | Nutrition & Meal Planning team                           |
| **Created** | 2026-04-17                                               |
| **Priority**| P1 within ADR-005 (biggest ADR-004 payoff; capstone for the "week-layer" workflow) |
| **Scope**   | Data model (`steps`, `CookEvent`), API endpoints (`cook-mode`, `cooked`), UI cook-mode view, integration with SPEC-015 pantry auto-debit, feedback stream to SPEC-011 |
| **Depends on** | SPEC-009 (recipe fields), SPEC-011 (feedback signals downstream), SPEC-015 (pantry hook, optional) |
| **Implements** | ADR-005 §5 (cook mode + structured cooking feedback) |

---

## 1. Problem Statement

Recipes today ship as a rationale + ingredient list. Users then
leave the app to actually cook — they find the recipe on a phone
held with oily fingers, scroll to the wrong place, and have no way
to say "I made this" vs. "I didn't". The system has no adherence
data; it only has preference data filtered through the narrow
`FeedbackRecord` rating path.

This spec closes the loop. Two things ship together:

1. **Cook mode** — a step-ordered, timer-aware, hands-busy UI with a
   backend data model (`steps: list[CookStep]`) that the meal-
   planning agent can emit and the UI can render.
2. **Cooking events** — `POST /cooked` structured events recording
   `status ∈ {made, partial, skipped, swapped}`, servings_made,
   per-ingredient substitutions observed, and optional
   `FeedbackRecord`. This is the **data separation** of adherence
   from preference that ADR-006's dashboard depends on.

These are coupled because cook mode is where the event naturally
gets emitted. Building them together also produces 10× the
feedback volume at roughly the same user effort as today's rating
dialog — which is the capstone payoff for ADR-004 and the
prerequisite for ADR-006.

---

## 2. Current State

### 2.1 Today

- `MealRecommendation` carries ingredients and a rationale, no
  structured steps.
- `FeedbackRecord` carries `rating`, `would_make_again`, and
  `notes`. One submission per recommendation, no concept of "did
  the user actually cook it."
- No cook-mode endpoint or UI.

### 2.2 Gaps

1. No structured steps to render a step-through UI.
2. No timers tied to steps.
3. No adherence data; we cannot tell "the user skipped Tuesday's
   dinner" from "the user cooked it and hated it."
4. ADR-006's entire goal-progress dashboard depends on the
   adherence denominator that does not exist yet.
5. ADR-004's preference extractor sees only rating-based feedback;
   cook events would roughly 10× the signal volume.

---

## 3. Goals and Non-Goals

### 3.1 Goals

- Extend `MealRecommendation` with `steps: list[CookStep]` — an
  additive field the meal-planning agent emits and the UI renders.
  Absent steps fall back to rationale-only mode.
- Ship `GET /plan/meals/{plan_id}/recipes/{rec_id}/cook-mode`
  returning the data a cook-mode UI needs: ordered steps, timers,
  ingredients keyed to steps, nutrients per serving, allergen
  flags, substitutions committed so far.
- Ship `POST /plan/meals/{plan_id}/recipes/{rec_id}/cooked` that
  records a structured `CookEvent` with adherence status and
  optional preference feedback.
- Feed `CookEvent` into SPEC-011's extractor so cooking signals
  update learned preferences (SPEC-012) and the retrieval index
  (SPEC-013).
- If the profile has `pantry_auto_debit=true` (SPEC-015), decrement
  pantry by the recipe's ingredient quantities on `status=made`.
  Off by default.
- Produce a separate `adherence_snapshot` row per cook event —
  the input ADR-006 reads.

### 3.2 Non-goals

- **No voice control or wake words.** v1 is a tap UI. Voice is a
  rich capability for a separate team.
- **No image capture of cooked dishes.** v1 text-only.
- **No social sharing.** v1 privacy-first; "share what I made" is
  a future opt-in.
- **No live multi-user cooking.** Single-user per session.
- **No live timer state across devices.** Timers are local; server
  persists only the event completion.

---

## 4. Detailed Design

### 4.1 Additive data model

```python
class CookStep(BaseModel):
    order: int                           # 1-based
    text: str                            # "Dice the onion and sauté until translucent."
    timer_seconds: Optional[int] = None  # inline timer
    ingredient_canonical_ids: List[str] = []
    tool_tags: List[str] = []            # 'knife', 'oven', 'pan'
    warning: Optional[str] = None        # "Oil is hot; be careful."

class MealRecommendation(BaseModel):
    # existing...
    steps: List[CookStep] = []
    equipment_tags: List[str] = []       # 'oven', 'blender', 'grill'
```

All additive; legacy recipes without `steps` fall back to the
current rationale-only card. The meal-planning agent prompt is
extended to emit `steps` (numbered, ~6–12 per recipe, with
inline timer_seconds where appropriate). Structured-output
schema enforces shape.

### 4.2 Cook-mode API

```python
@dataclass(frozen=True)
class CookModePayload:
    recipe_id: str
    display_name: str
    steps: tuple[CookStep, ...]
    ingredients: tuple[IngredientChip, ...]    # including pantry-on-hand flag
    nutrients_per_serving: dict[Nutrient, float]
    clinical_flags: tuple[str, ...]            # from SPEC-007
    committed_substitutions: tuple[CommittedSub, ...]   # from SPEC-016
    portions_servings_numeric: float
    prep_time_minutes: Optional[int]
    cook_time_minutes: Optional[int]
```

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/plan/meals/{plan_id}/recipes/{rec_id}/cook-mode` | Returns `CookModePayload` |

Read-only. Computes on demand from stored recipe + SPEC-016
committed subs + SPEC-015 pantry; cached per `(recipe_id,
recipe_updated_at)`.

### 4.3 Cooked event API

```python
class Substitution(BaseModel):
    from_canonical_id: str
    to_canonical_id: str
    note: Optional[str] = None

class CookedEventRequest(BaseModel):
    status: Literal["made", "partial", "skipped", "swapped"]
    servings_made: Optional[float] = None
    substitutions: List[Substitution] = []
    feedback: Optional[FeedbackRecord] = None
    occurred_at: Optional[str] = None            # ISO; defaults to now

class CookEvent(BaseModel):
    cook_event_id: str
    recipe_id: str
    plan_id: str
    client_id: str
    status: str
    servings_made: Optional[float]
    substitutions: List[Substitution]
    feedback: Optional[FeedbackRecord]
    occurred_at: str
    recorded_at: str
    effective_nutrients: Dict[Nutrient, float]    # computed per §4.5
```

| Method | Path | Purpose |
|--------|------|---------|
| `POST`   | `/plan/meals/{plan_id}/recipes/{rec_id}/cooked` | Record a cooking event |
| `GET`    | `/plan/meals/{plan_id}/cooking-events` | List events for the plan |
| `PATCH`  | `/plan/meals/cooking-events/{cook_event_id}` | Correct a recent event |
| `DELETE` | `/plan/meals/cooking-events/{cook_event_id}` | Retract within 24 h |

Correction / retraction exists because users mis-tap; beyond 24 h
cook events become authoritative and corrections are admin-only.

### 4.4 Status semantics

- **`made`** — user cooked the recipe as planned; nutrient
  contribution is the full recipe × `servings_made /
  portions_servings_numeric`.
- **`partial`** — started cooking but incomplete (ran out of
  time, swapped mid-recipe). Requires `servings_made`. Nutrient
  contribution scales proportionally.
- **`skipped`** — did not cook; nutrient contribution is zero.
  **Crucially** we do not assume the user ate nothing — off-plan
  intake goes through ADR-006's quick-log (separate spec).
- **`swapped`** — replaced with a different recipe outside our
  plan. The user provides a `substitution` at the recipe level;
  nutrient contribution is the substitute's best-effort estimate
  (LLM parse if user provided text, zero if not).

### 4.5 Effective nutrients

On `POST /cooked`:

1. Start with the recipe's nutrients (from SPEC-009 on the current
   stored recipe — includes SPEC-016 substitutions already).
2. Apply additional per-event substitutions from the request
   (quantified via SPEC-005 densities + SPEC-008 nutrients).
3. Scale by `servings_made / portions_servings_numeric`.
4. Status `skipped` → all zeros.

The computed `effective_nutrients` is persisted on the event and
is what ADR-006's **eaten rollup** reads. It is distinct from the
recipe's static nutrients.

### 4.6 Downstream integrations

**SPEC-011 (feedback signals).** Every `CookEvent` with a non-
skipped status produces a `FeedbackSignals` call:

- `status=made` with `FeedbackRecord` → same as today's feedback,
  but the note now has much richer context.
- `status=partial` → a derived signal on `prep_time` dimension:
  weekday→negative if on weekdays, weekend→neutral. Evidence:
  "partial, ran out of time".
- `status=swapped` with substitutions → ingredient-affinity signals
  on the swapped ingredients.

SPEC-011's extractor is extended with a `CookedEventContext` path
that treats cook-event-derived signals as higher-confidence than
rating-only signals (the user physically cooked it; that's a
stronger signal than tapping stars).

**SPEC-015 (pantry auto-debit).** If
`profile.pantry_auto_debit=true` and `status ∈ {made, partial}`,
decrement `nutrition_pantry.quantity_grams` by the recipe's
parsed ingredient masses × servings ratio. Decrements that would
go negative clamp to 0 and log. Off by default; opt-in from the
profile UI.

**ADR-006 (adherence ledger).** The **adherence snapshot** row
for ADR-006 is written here inline:

```sql
INSERT INTO nutrition_adherence_events (
    client_id, plan_id, recipe_id, occurred_at,
    status, servings_made, effective_nutrients_json
) VALUES ...
```

ADR-006 defines the schema; this spec provides the write call.
Table is created in ADR-006's first spec — we reference its
shape here for alignment.

### 4.7 Persistence

Migration `014_cook_events.sql`:

```sql
CREATE TABLE IF NOT EXISTS nutrition_cook_events (
    cook_event_id          TEXT PRIMARY KEY,
    client_id              TEXT NOT NULL,
    plan_id                TEXT NOT NULL,
    recipe_id              TEXT NOT NULL,
    status                 TEXT NOT NULL,
    servings_made          DOUBLE PRECISION,
    substitutions_json     JSONB NOT NULL DEFAULT '[]'::jsonb,
    effective_nutrients_json JSONB NOT NULL,
    feedback_json          JSONB,
    occurred_at            TIMESTAMPTZ NOT NULL,
    recorded_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    retracted              BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX ON nutrition_cook_events (client_id, occurred_at DESC);
CREATE INDEX ON nutrition_cook_events (plan_id);
CREATE INDEX ON nutrition_cook_events (recipe_id);
```

### 4.8 UI

**Cook mode view** (dedicated fullscreen):

```
[<]  Shakshuka                                       [x]
Step 3 of 8                                  [~ timer: 4:20]

Dice the onion and sauté until translucent.

Ingredients for this step:
 [ ] 1 medium onion (in your pantry ✓)
 [ ] 2 tbsp olive oil

⚠ Contains high sodium (flagged for your HTN plan).

              [ Prev ]        [ Next ]

At any time:  [ I made this ]  [ I skipped this ]
```

Principles:

- Large type, high contrast, hands-busy friendly.
- One step visible at a time, with prev/next. Full list
  accessible via a tap.
- Timers inline; tap to start, they count down visibly.
- "I made this" and "I skipped this" are persistent actions
  accessible from any step.
- Substitution action ("I used X instead") deep-links into
  SPEC-016's dialog.
- Nutrient summary available in a pull-down tray; not primary
  UI.
- Copy reviewed against ADR-006 §6.5 guidelines (never shame-
  framed; the "skipped" action is neutrally worded: "I didn't
  cook this one").

**Cooked-event prompts** on plan screen:

- After a recipe's `suggested_date` + mealtime window passes and
  no cook event has been recorded, the plan screen shows a single
  inline prompt next to that recipe: *"Did you cook this?"* with
  three taps: [Made it] [Skipped] [Later]. "Later" snoozes until
  the next open.
- One-tap logging is critical to volume; a heavier dialog would
  cause the same fatigue as today's rating UI.

### 4.9 Cook-mode entry points

1. From the plan screen: recipe card → "Cook" button.
2. From the calendar (SPEC-017): event description deep link.
3. From the grocery list (SPEC-014): "start cooking" quick action
   on any recipe ready to cook.

All three arrive at the same `GET /cook-mode` endpoint.

### 4.10 Observability

- `cook.event.recorded{status}`.
- `cook.event.correction_window` histogram (how long after
  occurred_at users correct).
- `cook.event.substitutions_per_event` histogram.
- `cook.mode.opened` per recipe.
- `cook.mode.step_advance` — rough engagement metric (do users
  actually step through?).
- `cook.event.feedback_attached_rate` — fraction of events with
  FeedbackRecord; compare to today's rating rate.
- `cook.adherence.made_rate` per user week — a core ADR-006
  dashboard metric.

### 4.11 Privacy

- Cook events are user activity data; retained per profile's
  retention policy and deleted on account deletion.
- Step text and note content are not logged above DEBUG.
- Timer metadata (how long users took on each step) is **not
  stored** in v1 — it is stepping into surveillance territory
  without clear user value. Backlogged as opt-in for users who
  want timing estimates.

### 4.12 Priority-grouped work items

| # | Item | Priority |
|---|------|----------|
| W1 | `steps`, `equipment_tags` additive on `MealRecommendation`; planner prompt update with structured-output schema | P0 |
| W2 | Migration `014_cook_events.sql` | P0 |
| W3 | `GET /cook-mode` endpoint + payload assembly | P0 |
| W4 | `POST /cooked` endpoint + effective-nutrients computation | P0 |
| W5 | Correction/retraction endpoints | P1 |
| W6 | SPEC-011 extractor wiring for cook events | P0 |
| W7 | SPEC-015 pantry auto-debit hook | P1 |
| W8 | ADR-006 adherence-event write (coordinated with ADR-006 spec) | P0 |
| W9 | UI: cook-mode fullscreen view | FE | P1 |
| W10 | UI: inline "did you cook this?" prompt | FE | P0 |
| W11 | UI: substitution deep link from cook mode | FE | P1 |
| W12 | Observability counters | P1 |
| W13 | Planner prompt tuning iteration on `steps` output quality | P1 |
| W14 | Benchmarks: `GET /cook-mode` p99 ≤ 150 ms; `POST /cooked` p99 ≤ 200 ms | P2 |

---

## 5. Rollout Plan

Two flags, independent:

- `NUTRITION_COOKING_EVENTS` (off → `/cooked` hidden; on →
  endpoint live + inline prompt shown).
- `NUTRITION_COOK_MODE_UI` (off → cook-mode view hidden; on →
  view accessible).

Cooking events are intentionally shippable before the cook-mode
UI — the one-tap "did you cook this?" prompt is the highest-
ratio capability in the spec and should land first.

### Phase 0 — Foundation (P0)
- [ ] W1, W2 landed; migration in staging.

### Phase 1 — Cooking events behind flag (P0)
- [ ] W4, W6, W8, W10 landed.
- [ ] Flag `NUTRITION_COOKING_EVENTS` on internal.
- [ ] Measure: what fraction of internal users' plans get at
      least one `made/skipped` tap per week? Target ≥60%.

### Phase 2 — Cook mode behind flag (P1)
- [ ] W3, W9, W11 landed.
- [ ] Flag `NUTRITION_COOK_MODE_UI` on internal.
- [ ] Acceptance gate: team members use cook mode for ≥5 meals
      each; UX polished enough that "oily-hands" navigation feels
      right.

### Phase 3 — Extractor + pantry (P1)
- [ ] W6 signals flowing to SPEC-011.
- [ ] W7 pantry auto-debit opt-in available; off by default.
- [ ] Monitor SPEC-012 digest shifts under the new signal volume.

### Phase 4 — Ramp (P1)
- [ ] 10% → 50% → 100% of both flags over three weeks.
- [ ] Watch `cook.adherence.made_rate` and
      `cook.event.feedback_attached_rate` — the two capstone
      metrics.

### Phase 5 — Cleanup (P1/P2)
- [ ] W5 correction endpoints.
- [ ] W12, W13, W14 — observability, prompt tuning, benchmarks.
- [ ] Flag defaults on; removal scheduled.

### Rollback
- Either flag off → hides capability; persisted events remain and
  continue feeding ADR-006 / SPEC-011 (which can tolerate gaps).
- Additive migration.

---

## 6. Verification

### 6.1 Unit tests

- `test_effective_nutrients_made.py` — full servings → full
  nutrients.
- `test_effective_nutrients_partial.py` — half servings → half
  nutrients.
- `test_effective_nutrients_skipped.py` — all zeros regardless
  of recipe contents.
- `test_effective_nutrients_swap.py` — event-time substitutions
  override recipe-stored substitutions.
- `test_cooked_retraction.py` — retract within 24 h zeros adherence
  ledger; beyond 24 h admin-only.

### 6.2 Integration tests

- `test_cook_mode_payload.py` — payload includes SPEC-016
  committed subs and SPEC-015 pantry-on-hand flags.
- `test_cooked_flows_to_signals.py` — cook event with feedback
  produces a `FeedbackSignals` row with cook-event-derived source.
- `test_pantry_auto_debit.py` — with flag on, pantry decremented
  proportionally; with flag off, untouched.
- `test_adherence_event_written.py` — cook event writes an
  adherence-ledger row readable by ADR-006 fixtures.
- `test_correction_updates_signals.py` — retracting a made event
  emits a compensating signal (or drops the original extraction)
  so SPEC-012 preferences remain consistent.

### 6.3 Planner prompt tests

- `test_planner_emits_steps.py` — for a corpus of 20 generations,
  ≥90% return at least 6 structured steps; fewer than 3 produce
  no steps at all.
- `test_steps_ingredient_mapping.py` — `ingredient_canonical_ids`
  in steps reference ingredients that exist in the recipe's
  ingredient list.

### 6.4 UX verification (Phase 2)

- 10 team members use cook mode for at least 5 real meals.
- Open-ended review finds no critical navigation issues (e.g.
  cannot advance step, timer does not reset correctly).

### 6.5 Observability

All §4.10 counters emit; dashboards for adherence rate and
feedback-attached rate populated.

### 6.6 Cutover criteria

- All P0/P1 tests green.
- Phase 1 internal adherence-tap rate ≥60%.
- Phase 2 UX review passes.
- Phase 4 ramp: adherence-tap rate at 10% stable → scale
  progressively; no correctness incidents.

---

## 7. Open Questions

- **Per-step completion tracking.** We store the cook event but
  not per-step completion. Adding it is cheap storage but has
  privacy implications (timing data). v1 keeps it off; add
  opt-in later if users request "how long did I take last time?"
- **Leftover capture.** "Servings made=4, servings eaten=2" →
  2 servings leftover. A natural pantry add. Tracking requires a
  separate `servings_eaten` field and a pantry category for
  leftovers. Backlogged as v1.1.
- **Multi-recipe cooking sessions.** Cooking a meal-prep Sunday
  is really multiple recipes in parallel. v1 is single-recipe
  cook mode; multi-recipe sessions are a UX extension.
- **Offline cook mode.** Kitchens are sometimes out of Wi-Fi
  range. v1 loads the payload once and does not require live
  connection for navigation; the POST /cooked is queued if the
  network is down and retried. Service-worker details are a
  frontend implementation concern.
- **"I'm about to cook this" start event.** Logging cook-start
  would unlock nice UX (reminder dismissal, pantry soft-hold).
  v1 tracks only completion; start tracking is v1.1.
