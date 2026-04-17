# ADR-003 — Quantitative meal-plan validation: per-recipe nutrient rollup and targeted repair

- **Status**: Proposed
- **Date**: 2026-04-17
- **Owner**: Nutrition & Meal Planning team
- **Related**: ADR-001 (deterministic daily targets), ADR-002 (ingredient canonicalization), `backend/agents/nutrition_meal_planning_team/`

## Context

Today the meal-planning agent returns `MealRecommendation` items with
`ingredients: List[str]`, a rationale sentence, and no nutrient data.
The orchestrator records each suggestion and returns them — nothing
verifies that the week's meals add up to the `NutritionPlan.daily_targets`.
`DailyTargets` and the meal plan are produced in sequence but never
reconciled.

This is the reason "a plan" feels like "a list of suggestions". Three
concrete consequences:

1. **The product cannot make a goal claim.** We cannot tell a user on
   a 1,700 kcal / 130g protein target whether the plan we served them
   actually lands there — or whether Tuesday is 900 kcal and Friday is
   2,600. Adherence tracking, weight-loss progress, and diabetes carb
   distribution all depend on this number existing.
2. **Clinical cohorts are unserved.** CKD protein caps, hypertension
   sodium caps, and T2D carb-per-meal distribution are per-meal or
   per-day *quantitative* constraints. ADR-001's calculator produces
   them; nothing here checks them. The calculator is toothless without
   a rollup.
3. **No substitution surface.** When a user rejects Tuesday dinner, we
   have no way to ask for a replacement that "hits ≥35 g protein and
   ≤800 mg sodium" — because we do not know what the current Tuesday
   dinner contains either.

ADR-002 introduces canonical ingredient ids and a parser. That
unlocks deterministic nutrient lookup; this ADR defines how we use it.

## Decision

Add a **nutrient-rollup** stage between `meal_planning_agent.run` and
`_record_suggestions`, and a **targeted-repair** loop that swaps
specific meals when daily totals fall outside tolerance bands.

### 1. Per-ingredient nutrient data

Extend `ingredient_kb/` (ADR-002) with a nutrient table. Source of
truth: USDA FoodData Central (FDC) SR Legacy + Foundation Foods +
Branded Foods for packaged items. Cached in Postgres:

- `ingredient_nutrients(canonical_id, nutrient_id, per_100g)` — macros
  plus the micros ADR-001 targets (fiber, sodium, potassium, iron,
  calcium, vitamin D, B12, phosphorus for CKD, vitamin K for warfarin).
- `ingredient_density(canonical_id, unit, grams)` — household-unit to
  gram conversions (1 cup spinach raw = 30 g; 1 tbsp olive oil = 13.5 g)
  so we can convert parsed quantities to mass.
- Refreshed via a scheduled job; version-stamped for cache
  invalidation.

### 2. Recipe nutrient computation

Add `nutrition_meal_planning_team/nutrient_rollup/` with:

- `recipe.py::compute_recipe_nutrients(rec: MealRecommendation)
  -> RecipeNutrients` — for each parsed ingredient, resolve qty to
  grams via density table, multiply by `per_100g`, sum, then divide by
  `portions_servings` to get per-serving values.
- Handling for cooked-vs-raw (water loss) via a small retention-factor
  table on `canonical_foods.yaml` for items where it matters most
  (meat, pasta, rice, leafy greens).
- `confidence: float` on each `RecipeNutrients`, driven by the share
  of ingredient mass successfully resolved. Low-confidence recipes are
  surfaced, not silently assumed correct.

`MealRecommendationWithId` gains:
```
nutrients_per_serving: RecipeNutrients
nutrients_confidence: float
```

### 3. Plan-level rollup

Add `plan.py::rollup_plan(suggestions, period_days) -> PlanRollup`:

- `by_day: dict[date, DailyTotals]` — sums the suggestions assigned to
  each day via `suggested_date`.
- `variance_vs_target: dict[date, dict[nutrient, pct_delta]]` — each
  nutrient's delta vs `DailyTargets` from ADR-001.
- `weekly_totals` and `weekly_variance` for nutrients where weekly
  adequacy matters more than per-day (vitamin D, iron).
- `per_meal_caps_breached: list[Breach]` — per-meal sodium, carb, or
  phosphorus caps from clinical overrides.

### 4. Tolerance policy

Configurable per-nutrient tolerances, with clinically tighter defaults:

- Calories: ±10% day, ±5% week.
- Protein: −10% / +25% day.
- Carbs: AMDR band enforced; T2D profile: per-meal carb cap from
  calculator.
- Sodium: cap-only; violation if `day > cap` or `meal > per_meal_cap`.
- Fiber, K, Ca, Fe, D, B12: weekly adequacy, ≥80% of target averaged.
- Saturated fat: cap at 10% of kcal (AHA default; overridable).

Tolerances live in `nutrient_rollup/tolerances.yaml`, versioned the
same way as `canonical_foods.yaml`.

### 5. Targeted-repair loop

If the rollup surfaces violations:

1. Rank violations by severity (hard cap breach > macro out-of-band >
   micro adequacy gap).
2. For each violation, build a **minimal swap request** targeting the
   specific day and meal(s) that move the totals back in-band — e.g.
   "replace Tuesday lunch with a meal that has ≥35 g protein, ≤800 mg
   sodium, ≤45 g carbs, prep ≤20 min, no shellfish".
3. Call the meal-planning agent with a single-suggestion prompt
   (concurrency across independent swaps).
4. Swaps pass through the ADR-002 guardrail unchanged.
5. Re-run the rollup. Stop when all violations resolve or
   `MAX_REPAIR_ITERS` (default 2) is reached. Remaining gaps are
   surfaced, not hidden.

This is explicitly *swap*, not *regenerate*: users hate losing the
meals they already liked on the screen. Only the specific offending
meals move.

### 6. API surface

- `MealPlanResponse` gains:
  ```
  rollup: PlanRollup
  repair_history: list[RepairAttempt]   # what we swapped and why
  ```
- New endpoint `POST /plan/meals/{id}/swap` — swap a single meal the
  user rejected, using the same single-suggestion path; returns the
  new recommendation plus the updated rollup.
- `GET /history/meals` entries gain `nutrients_per_serving` so past
  meals can be re-analyzed without re-derivation.

### 7. Caching and cost

- Recipe-level nutrient rollups are content-addressed by a hash of
  `(canonical_ingredient_ids, quantities, portions)` and cached.
  Repeated recipes (and the substitution agent) hit cache.
- FDC lookups are local (Postgres). Zero per-request network cost.
- Repair-loop LLM calls are single-suggestion, so worst-case latency
  is `MAX_REPAIR_ITERS × swap_count` extra short calls, parallelized.

## Consequences

### Positive

- The plan becomes a plan. Users see daily totals and a clear "you're
  meeting your targets" state — the largest perceived-quality jump
  available to us.
- Clinical calculator clamps (ADR-001) become enforceable:
  protein-capped CKD diets, sodium-capped HTN diets, and carb-
  distributed T2D diets actually behave that way.
- Substitution UX (ADR-005) gets its targeting primitive for free.
- Adherence / goal-tracking data is now first-class: we can show
  weekly deviation and correlate with weight trends once that
  workstream lands.
- Low-confidence recipes get flagged, not silently trusted — failure
  mode is visible.

### Negative / costs

- **FDC coverage and parse quality are the load-bearing inputs.**
  Rollups are only as good as ingredient resolution (ADR-002) and the
  density table. Early confidence scores will be mixed. We mitigate by
  refusing to make strong claims on low-confidence plans and by using
  rejections as the backlog signal for KB work.
- **Latency floor rises.** Even cache-hit paths add rollup compute and
  a variance check; cache-miss paths add one LLM swap per violation.
  Budget: +300–800 ms typical, +2–4 s worst case on the async path.
  Mitigation: the async `/plan/meals/async` endpoint already exists;
  the sync path remains for small plans.
- **Moving the goalposts changes behavior.** Users who today see a
  plan instantly will sometimes see a "we adjusted Tuesday to hit your
  protein target" message. That is a feature, not a regression, but
  needs clear UX so it does not feel like indecision.
- **Tolerance tuning is political.** Any number we pick will argue
  with someone's diet philosophy. We keep tolerances in a versioned
  config file and expose per-profile overrides for advanced users;
  defaults are conservative and cited.
- **Repair loops can get stuck.** A profile with tight allergies +
  tight clinical clamps + tight time budget may be infeasible.
  `MAX_REPAIR_ITERS` plus visible unresolved gaps prevents silent
  thrashing; we will iterate on the surface.

### Neutral / follow-ups

- Substitution endpoint and cook-mode feedback (ADR-005) both build
  directly on this ADR's rollup + swap primitives.
- Adherence tracking ("did the user actually cook it?") is a separate
  workstream; this ADR gives it the denominator it needs.
- A future optimization pass can replace the greedy targeted-repair
  loop with a small ILP over candidate swaps when the catalog grows;
  out of scope for v1.
