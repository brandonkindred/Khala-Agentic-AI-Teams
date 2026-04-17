# ADR-005 — Actionable workflow layer: grocery list, pantry, substitutions, calendar, cook mode

- **Status**: Proposed
- **Date**: 2026-04-17
- **Owner**: Nutrition & Meal Planning team (+ Integrations team for calendar)
- **Related**: ADR-002 (canonical ingredients), ADR-003 (nutrient rollup + swap), ADR-004 (learned preferences), `backend/agents/nutrition_meal_planning_team/`, `backend/agents/integrations/`

## Context

The team currently ends at `MealPlanResponse`: a list of
`MealRecommendationWithId`. From the user's point of view, getting the
list is step one of about eight:

1. Read the list.
2. Mentally reconcile overlapping ingredients (three recipes call for
   red onion — how many do I need?).
3. Subtract what is already in the pantry.
4. Build a shopping list, grouped by supermarket section.
5. Go shopping.
6. Realize on Tuesday they are out of Greek yogurt; figure out what to
   substitute.
7. Remember to start Tuesday dinner in time to eat by 7.
8. Follow the recipe while their hands are covered in olive oil.

Steps 2–8 are where users churn. The nutrition team owns the hardest
part (the plan) and cedes the part users actually live in (the week).
Every consumer meal-planning product that retains users wins by
owning that week-layer, not by having better recommendations.

Two enabling shifts in this roadmap make the workflow layer feasible
now:

- ADR-002 canonicalizes ingredients (so aggregation and pantry
  subtraction are deterministic).
- ADR-003 attaches nutrients to each recipe and exposes a swap
  primitive (so substitutions are nutrient-aware, not just
  ingredient-aware).

This ADR specifies the workflow layer on top.

## Decision

Add five coordinated capabilities, each a thin layer on the primitives
already established in ADR-001–004. Explicitly: **no new LLM agents
are strictly required for v1**; the existing meal-planning agent,
plus deterministic aggregation and the Integrations team, covers it.

### 1. Consolidated grocery list

- New endpoint: `POST /plan/meals/{plan_id}/grocery-list` returning a
  `GroceryList` grouped by supermarket section.
- Aggregation uses parsed, canonicalized ingredients from ADR-002 and
  the density table from ADR-003:
  1. Parse every ingredient line in the plan.
  2. Convert all quantities to canonical units (g or ml) via density.
  3. Sum by `canonical_id`.
  4. Convert back to **purchase units** (a "red onion" is a count, not
     100 g) via a `purchase_unit` field on `canonical_foods.yaml`.
  5. Round up with a configurable waste-buffer (default 10%).
  6. Group by `aisle_tag` (`produce`, `dairy`, `pantry`, `meat_fish`,
     `frozen`, `bakery`, `spices`, `other`).
- Persisted as `grocery_lists` in Postgres; re-generatable on demand.
- Exportable: plain text, CSV, and a signed deep-link the UI can pass
  to third-party cart integrations later (Instacart, Amazon Fresh —
  out of scope for v1 but the data shape supports it).

### 2. Pantry

- New store: `pantry_items(client_id, canonical_id, quantity_g,
  unit_display, expires_on?)`.
- Endpoints: `GET/POST/PUT/DELETE /pantry/{client_id}`; bulk
  `POST /pantry/{client_id}/import` from a text dump (LLM-parsed via
  the ADR-002 parser, surfaced to the user for confirmation — we do
  not silently mutate pantry).
- Grocery-list generation subtracts pantry quantities **before**
  rounding; the shortfall is what lands on the list.
- Cooking a recipe (see §5) optionally decrements pantry by the
  recipe's ingredient quantities. Off by default; opt-in per user
  because auto-pantry-debit is a trust decision.
- Near-expiry items (≤3 days) surface on `POST /plan/meals` as hints:
  the planner prompt gets a short "prefer meals using: tofu,
  spinach, yogurt (expiring soon)" line. Cheap and reduces waste.

### 3. Substitution

- New endpoint:
  `POST /plan/meals/{plan_id}/recipes/{rec_id}/substitute` with body
  `{out_of: [canonical_id], have_instead?: [canonical_id], reason?: str}`.
- Flow:
  1. Build a single-suggestion LLM call with the full recipe plus
     explicit constraint ("without `greek_yogurt`; acceptable swaps
     from pantry: `sour_cream`, `silken_tofu`; otherwise any
     nutritionally similar option").
  2. Pass through the ADR-002 guardrail.
  3. Run nutrient rollup (ADR-003) on the swapped recipe; report the
     delta ("calories −40, protein −6 g, calcium −120 mg").
  4. If the swap breaks a per-meal cap or pushes the day out of
     tolerance beyond `SUB_TOLERANCE`, return with `warn=true` and
     let the user decide.
- A lightweight deterministic fallback for the common 20 swaps
  (yogurt↔sour cream, butter↔olive oil for sautéing, milk↔oat milk,
  etc.) is tried before the LLM call to cut latency.

### 4. Calendar + reminders

- Reuse the Integrations team's Google Calendar client (the team
  already owns Google OAuth; see CLAUDE.md on `shared` Google browser
  login). No new OAuth surface.
- New endpoints:
  - `POST /plan/meals/{plan_id}/calendar/sync` — creates events for
    each recipe's `suggested_date` + mealtime window, with
    prep-time-aware reminders (`reminder = mealtime -
    (prep_time + cook_time + buffer)`).
  - `DELETE /plan/meals/{plan_id}/calendar/sync` — removes them.
- Event body contains a deep link to the cook-mode view (§5) plus
  ingredient checklist and nutrient summary.
- Respect user-configured mealtime windows from the profile
  (`preferences.mealtime_windows` — small profile addition).
- Per-event reminders use local timezone from the profile (adds a
  `timezone` field; tiny migration).

### 5. Cook mode + structured cooking feedback

- UI-side primarily, but the backend exposes the data model:
  - `GET /plan/meals/{plan_id}/recipes/{rec_id}/cook-mode` — returns a
    step-ordered structure: `{ingredients, steps, timers,
    nutrients_per_serving, safety_flags}`. Steps come from the
    recipe's `steps: list[CookStep]` — a new field on
    `MealRecommendation`; the meal-planning prompt is updated to emit
    it (non-breaking: absence falls back to rationale-only).
  - `POST /plan/meals/{plan_id}/recipes/{rec_id}/cooked` with body
    `{status: "made"|"partial"|"skipped"|"swapped",
      servings_made, feedback?: FeedbackRecord,
      substitutions?: [...]}`.
- The `cooked` event is the data-layer key change: it separates
  **adherence** ("did they cook it?") from **preference** ("did they
  like it?"). Today we conflate them.
- Emits `FeedbackRecord` into the existing feedback pipeline
  unchanged, and emits structured cooking events into ADR-004's
  extractor with much richer signal ("partial, ran out of time" →
  `effort_tolerance` downward weekday; "swapped tofu for chicken,
  loved it" → strong protein-affinity signal).
- Cook-mode can decrement pantry (§2) on "made" if the user opted in.

### 6. Data model additions (summary)

New / modified:

- `MealRecommendation.steps: list[CookStep]` (additive, optional).
- `MealRecommendation.purchase_unit_hints: dict[canonical_id, unit]`
  — optional, helps grocery aggregation when the recipe specifies a
  different unit than the canonical purchase unit.
- `grocery_lists`, `pantry_items`, `cooking_events` Postgres tables,
  registered via `shared_postgres.register_team_schemas`.
- `preferences.mealtime_windows` on `ClientProfile`, `timezone` on
  `ClientProfile`.

All additive; no breaking changes to the existing API responses.

### 7. Rollout order

Not a hard dependency order, but recommended:

1. Grocery list (depends on ADR-002 parser; everything else waits on
   this).
2. Pantry (depends on grocery list aggregation).
3. Substitution (depends on ADR-002 + ADR-003).
4. Cook mode data model + `cooked` event (parallel with 2–3; biggest
   ADR-004 payoff).
5. Calendar sync (depends on mealtime windows + timezone; low risk,
   high perceived value, good last-mile launch).

## Consequences

### Positive

- Converts the team from a recommendation engine into a **weekly
  operating system**. This is where consumer-health retention lives.
- Every step up (list → shop → swap → cook → feedback) generates
  structured data that improves the system, compounding with ADR-004.
- Cook mode's `cooked` event separates adherence from preference,
  which unlocks honest goal-progress claims (did the user cook the
  plan that was supposed to get them to their goal?).
- Near-expiry hints and pantry subtraction measurably reduce
  household food waste — a genuine, shippable health-plus-
  sustainability story.
- Each capability is independently valuable. We can ship in sequence
  and get user value at each step; no big-bang launch.

### Negative / costs

- **Largest scope of the five ADRs.** Five capabilities, new tables,
  new endpoints, UI surface area, and an integration (calendar).
  Mitigation: explicit rollout order (§7) and all capabilities are
  additive — nothing here is a prerequisite for the rest of the
  roadmap.
- **Pantry is a UX trap.** Users will love the idea, hate the data
  entry. Mitigation: opt-in auto-debit, bulk import, near-expiry
  hints to make it *useful* not just *maintained*, and fully usable
  with an empty pantry (it is a filter, not a requirement).
- **Grocery-list edge cases are long-tail.** "1 clove garlic" vs "1
  head of garlic" aggregation, spice-rack assumptions, bulk items,
  household-size scaling — none are individually hard, collectively a
  sustained polish cost. We will accept rough edges in v1 and
  prioritize aggregates that matter (produce, proteins).
- **Calendar sync has a blast radius.** A bad calendar write is very
  visible to the user. Mitigation: namespaced event descriptions
  (`[Khala Meals]`), dedicated calendar or clear opt-in, dry-run mode
  showing events before commit, idempotent write with plan-id tag.
- **Cook-mode step generation is new prompt surface.** The
  meal-planning agent has not had to emit step structures before;
  early outputs will be variable. Additive field means absence is
  harmless; we can iterate on the prompt without blocking ship.
- **Substitution LLM calls are the new cost center.** Common swaps are
  deterministic (the shortlist in §3) specifically to keep per-swap
  cost near zero; long-tail swaps hit the LLM. Acceptable.

### Neutral / follow-ups

- Third-party grocery fulfillment (Instacart / Amazon Fresh /
  Walmart+) is a clean v2 built on the exported `GroceryList` shape;
  intentionally out of scope.
- Recipe scaling for variable household sizes on a given day (guest
  coming for dinner) is a small, separable feature that drops in on
  top of the aggregation logic; tracked separately.
- A future ADR on **goal progress** ties together ADR-001 (targets),
  ADR-003 (rollup), ADR-004 (preferences), and ADR-005 (cooking
  events) into a weight/adherence dashboard. That is the capstone;
  not in scope here.
