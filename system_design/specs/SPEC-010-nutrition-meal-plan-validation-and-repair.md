# SPEC-010: Meal-plan validation, targeted repair, and swap endpoint

| Field       | Value                                                    |
|-------------|----------------------------------------------------------|
| **Status**  | Proposed                                                 |
| **Author**  | Nutrition & Meal Planning team                           |
| **Created** | 2026-04-17                                               |
| **Priority**| P0 (capstone for ADR-003; unblocks ADR-005 substitutions and ADR-006 eaten rollup) |
| **Scope**   | `backend/agents/nutrition_meal_planning_team/orchestrator/`, `agents/meal_planning_agent/`, `shared/meal_feedback_store`, `models.py` (additive), new `tolerances/` config, `user-interface/` meal-plan display |
| **Depends on** | SPEC-007 (guardrail still enforces on any output), SPEC-009 (recipe + plan rollup) |
| **Implements** | ADR-003 §4 (tolerance policy), §5 (repair loop), §6 (API), §7 (caching) |

---

## 1. Problem Statement

SPEC-009 computes per-recipe and per-plan nutrient totals and
reports variance against targets. SPEC-007 enforces allergen and
dietary restrictions. This spec is what connects them to the live
meal plan: when a plan's rollup violates tolerances from SPEC-003's
targets, which specific meal do we swap, how do we ask for a
replacement that actually closes the gap, and how do we do it
without throwing away the meals the user already liked on screen?

It is also where the `/swap` primitive lives — a single-suggestion
replacement endpoint reused by ADR-005's user-initiated
substitution.

This spec is the capstone of ADR-003. After it lands, "a meal plan"
is no longer a list of suggestions; it is a plan that provably
meets the user's targets within configurable tolerances, with any
remaining gaps visible rather than hidden.

---

## 2. Current State

### 2.1 Today's flow (post-SPEC-007)

```mermaid
sequenceDiagram
    participant API
    participant Orch as Orchestrator
    participant MPA as meal_planning_agent
    participant GR as guardrail
    participant Store

    API->>Orch: POST /plan/meals
    Orch->>MPA: run(profile, plan, history)
    MPA-->>Orch: List[MealRecommendation]
    loop per suggestion
        Orch->>GR: check_recommendation
        alt pass
            Orch->>Store: record
        else reject
            Orch->>MPA: regenerate_single
            Orch->>GR: re-check; record or drop
        end
    end
    Orch-->>API: MealPlanResponse (+ dropped)
```

No nutrient check anywhere. `DailyTargets` and the served meals
coexist but are never reconciled.

### 2.2 Gaps

1. Plan-level rollup is not computed.
2. No tolerance policy; no breach detection; no visible variance.
3. No repair step; a protein-deficient week ships as-is.
4. No `/swap` primitive; user-rejected meals cannot be replaced
   under a nutrient constraint.
5. Downstream work (ADR-006 eaten rollup, ADR-005 substitution,
   goal-progress drivers) depends on this surface existing.

---

## 3. Goals and Non-Goals

### 3.1 Goals

- On every `POST /plan/meals`, compute the plan rollup (SPEC-009)
  after the guardrail pass, detect tolerance breaches, and run a
  bounded targeted-repair loop that swaps specific meals to close
  specific gaps.
- Ship a tolerance configuration file that is reviewable,
  versioned, and per-profile overridable for advanced users.
- Return the rollup, the repair history, and any remaining
  unresolved breaches in the `MealPlanResponse` so the UI can
  render honest variance badges.
- Ship `POST /plan/meals/{plan_id}/recipes/{rec_id}/swap` as the
  single-suggestion replacement primitive. ADR-005's user-initiated
  substitution reuses this endpoint through an adapter.
- Keep performance acceptable: rollup + up to `MAX_REPAIR_ITERS=2`
  concurrent swap attempts should add ≤ 1.5 s to the sync path in
  the common case; worst case dispatches to the existing
  `/plan/meals/async` queue.
- Reuse SPEC-009's content-addressed recipe cache to avoid
  recomputing nutrients for repeated recipes and swaps.

### 3.2 Non-goals

- **No swap UX on its own.** ADR-005 covers user-initiated
  substitution, cook mode, and the grocery list. This spec exposes
  `/swap`; ADR-005 wires additional UX on top.
- **No learning loop from repairs.** ADR-004 owns that; we emit
  structured repair events it can subscribe to.
- **No eaten rollup or adherence.** ADR-006 — but the repair
  surface here is designed to be reused there.
- **No optimization beyond greedy repair.** A future ILP over
  candidate swaps is out of scope; v1 stays greedy with bounded
  iterations.

---

## 4. Detailed Design

### 4.1 New flow

```mermaid
sequenceDiagram
    participant API
    participant Orch as Orchestrator
    participant MPA as meal_planning_agent
    participant GR as guardrail
    participant RU as nutrient_rollup
    participant Tol as tolerances
    participant Store

    API->>Orch: POST /plan/meals
    Orch->>MPA: run
    MPA-->>Orch: suggestions[]
    Orch->>GR: _record_suggestions (SPEC-007)
    GR-->>Orch: recorded[] + dropped[]

    Orch->>Tol: build_for_profile(profile, plan.clinical_clamps)
    Orch->>RU: rollup_plan(recorded, targets, tolerances)
    RU-->>Orch: rollup with breaches
    loop up to MAX_REPAIR_ITERS
        alt no breaches
            break
        else breaches present
            Orch->>Orch: rank breaches, pick target recipe
            Orch->>MPA: regenerate_with_constraint(rec, breach)
            Orch->>GR: re-check
            alt pass
                Orch->>RU: rollup_plan (updated)
                RU-->>Orch: rollup
            else reject
                Orch->>Orch: record repair failure
            end
        end
    end
    Orch->>Store: persist rollup snapshot + repair history
    Orch-->>API: MealPlanResponse (rollup, repair_history, breaches_remaining)
```

### 4.2 Tolerance configuration

New file:
`backend/agents/nutrition_meal_planning_team/tolerances/defaults.yaml`

```yaml
version: "1.0.0"
per_day:
  kcal:           { lower_pct: -0.10, upper_pct: 0.10 }
  protein_g:      { lower_pct: -0.10, upper_pct: 0.25 }
  carbs_g:        { lower_pct: -0.15, upper_pct: 0.15 }   # band-only outside clinical overrides
  fat_g:          { lower_pct: -0.20, upper_pct: 0.20 }
  fiber_g:        { lower_pct: -0.20, upper_pct: null }
  sodium_mg:      { upper_pct: 0.10, cap_absolute: 2300 } # DRI upper
  saturated_fat_g: { upper_pct: 0.10, cap_absolute_pct_kcal: 0.10 }
per_meal:
  sodium_mg:      { cap_absolute: 800 }
  carbs_g:        { cap_absolute: null }                 # populated from clinical for T2D
per_week:
  iron_mg:        { lower_pct: -0.20, upper_pct: null }
  vitamin_d_mcg:  { lower_pct: -0.20, upper_pct: null }
  vitamin_b12_mcg: { lower_pct: -0.20, upper_pct: null }
  calcium_mg:     { lower_pct: -0.15, upper_pct: null }
  vitamin_k_mcg:  { lower_pct: -0.20, upper_pct: null }
```

Rules:

- `defaults.yaml` is the baseline. Clinical overrides from ADR-001
  (e.g. HTN sodium ≤ 1500 mg/day; CKD-3 per-meal phosphorus cap;
  T2D carbs-per-meal cap) compose on top.
- Per-profile overrides live on `ClientProfile.tolerance_overrides`
  (additive field) for advanced users who want tighter bands. No
  UI for this in v1; admin-only via `PATCH /profile`. UX consumer
  shows up in ADR-006 or later.
- `tolerances/resolver.py` has a single function
  `build_for_profile(profile, plan) -> Tolerances` that merges
  defaults, clinical overrides, and profile overrides
  deterministically. Merge order: defaults → clinical → profile
  (profile wins).
- Versioned; the `TOLERANCES_VERSION` constant is persisted on
  every rollup snapshot so a policy bump triggers visible repair
  recomputation (see §4.6 replay behavior).

### 4.3 Repair ranking

When breaches exist, pick the target recipe to swap:

1. Rank breaches by severity:
   - `cap_breach` (per-meal or per-day hard cap) → severity 3.
   - `band_miss` (day variance outside tolerance) → severity 2.
   - `adequacy_gap` (weekly adequacy) → severity 1.
2. For the top-ranked breach, identify contributing recipes:
   - Per-meal breach → the single recipe responsible (trivial).
   - Per-day breach → the recipe contributing the largest signed
     delta to that nutrient. Tie-break by lowest confidence, then
     by latest `suggested_date` (closer to "now" = more
     user-visible → swap later ones first to minimize churn on
     near-term days).
   - Per-week adequacy → recipe with the lowest per-serving value
     of the deficient nutrient (swap in something richer).
3. Skip recipes with `recipe_confidence < 0.7` (SPEC-009). Low-
   confidence recipes are not reliable swap targets; they get
   surfaced to the user with a "please clarify" chip.
4. Skip recipes the user has previously liked (rating ≥ 4 / would
   make again) via `meal_feedback_store`. These are preserved
   whenever possible; the repair loop prefers swapping recipes
   without explicit positive feedback.

### 4.4 Repair prompt

New `meal_planning_agent.regenerate_with_constraint(rec, breach,
profile, plan) -> Optional[MealRecommendation]`:

- Reuses SPEC-007's single-suggestion structured-output schema.
- Constraint block is explicit and numeric:
  ```
  Replace this Tuesday lunch. Keep meal_type=lunch, suggested_date=2026-04-21.
  Requirements:
    - ≥35 g protein (current recipe has 18 g).
    - ≤800 mg sodium per serving.
    - Prep + cook ≤ 20 minutes.
  Forbidden ingredients (from profile): cashew, peanut, ...
  Match the user's past preferences: one-pan meals, chicken or tofu, Mediterranean flavors.
  ```
- The agent emits one `MealRecommendation`; the orchestrator passes
  it through SPEC-007's guardrail before replacing.
- If the guardrail rejects the replacement twice consecutively, the
  repair attempt is abandoned; the original recipe stays and the
  breach is surfaced unresolved.

### 4.5 Loop bounds

- `MAX_REPAIR_ITERS = 2` whole-loop iterations (recompute rollup
  → repair → recompute → stop).
- Within a single iteration, all independent repairs (different
  target recipes) dispatch concurrently.
- Stop early if no breaches remain or if the last iteration made
  no forward progress (breach count did not decrease and the
  highest-severity breach did not drop).
- Total extra LLM calls per plan in the worst case: O(breaches)
  per iteration × 2 iterations. Budget: ≤ 10 regenerations
  total; anything beyond gets truncated with remaining breaches
  surfaced.

### 4.6 API changes

`MealPlanResponse` (additive):

```python
class RepairAttempt(BaseModel):
    iteration: int
    recipe_id: str
    replaced_recipe_id: Optional[str]
    breach_reason: str               # nutrient:scope e.g. "protein_g:per_day"
    outcome: Literal["succeeded", "guardrail_rejected", "llm_failed", "skipped"]
    delta: dict[str, float]          # change in nutrients after swap

class MealPlanResponse(BaseModel):
    client_id: str
    suggestions: List[MealRecommendationWithId]
    dropped: List[DroppedSuggestion]
    rollup: PlanRollup
    breaches_remaining: List[NutrientBreach]
    repair_history: List[RepairAttempt]
    tolerances_version: str
    guardrail_version: str
    rollup_version: str
```

New endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/plan/meals/{plan_id}/recipes/{rec_id}/swap` | Replace a single recipe; optional body `{reason?: str, constraint?: SwapConstraint}`. Returns the new recipe + updated plan rollup + per-swap delta |
| `GET`  | `/plan/meals/{plan_id}/rollup` | Returns current rollup snapshot (uses stored snapshot; does not recompute) |
| `POST` | `/plan/meals/{plan_id}/rerepair` | Force a fresh repair pass; rate-limited |

`SwapConstraint` lets callers specify additional constraints for
user-initiated swaps (e.g. "out of Greek yogurt; suggest a
replacement"). ADR-005's substitution path posts here with a
`SwapConstraint` describing the missing ingredient.

### 4.7 Persistence

Migration `006_plan_rollup_and_repair.sql`:

```sql
CREATE TABLE IF NOT EXISTS nutrition_plan_rollups (
    plan_id          TEXT PRIMARY KEY,
    client_id        TEXT NOT NULL,
    rollup_json      JSONB NOT NULL,            -- PlanRollup snapshot
    breaches_remaining JSONB NOT NULL,
    repair_history   JSONB NOT NULL,
    tolerances_version TEXT NOT NULL,
    rollup_version   TEXT NOT NULL,
    data_version     TEXT NOT NULL,             -- SPEC-008 version
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON nutrition_plan_rollups (client_id, computed_at DESC);

CREATE TABLE IF NOT EXISTS nutrition_recipe_nutrient_cache (
    cache_key         TEXT PRIMARY KEY,
    recipe_nutrients  JSONB NOT NULL,
    data_version      TEXT NOT NULL,
    rollup_version    TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON nutrition_recipe_nutrient_cache (data_version, rollup_version);
```

Recipe cache is populated on every rollup and read on swaps /
re-rollups. Invalidation is trivial: a version bump makes prior
rows stale; a periodic job garbage-collects rows whose versions
are older than the current pair.

### 4.8 Async path

Sync `/plan/meals` budget is: guardrail (existing SPEC-007
regeneration) + rollup + up to 2 × concurrent swaps.

Expected wall-clock:
- Rollup: ≤ 25 ms (SPEC-009 benchmark).
- Repair iteration with 2 concurrent swaps: ~1 × swap latency
  (meal LLM call, typically 1–3 s).
- Total sync budget: ≤ 6 s worst case.

If the orchestrator detects high repair pressure (e.g. breaches
> 4 at first rollup), it dispatches the full repair loop to the
existing `/plan/meals/async` path and returns a pending job
immediately. The `ClientProfile` has a `prefer_sync_plan` flag
(default true) the UI can honor.

### 4.9 Repair event stream

Each `RepairAttempt` emits a structured event to the team's
internal event bus:

```json
{
    "event": "nutrition.repair_attempt",
    "plan_id": "...",
    "client_id": "...",
    "iteration": 1,
    "breach_reason": "protein_g:per_day",
    "outcome": "succeeded",
    "delta": { "protein_g": 17.0, "sodium_mg": -200.0 },
    "tolerances_version": "1.0.0"
}
```

ADR-004 subscribes (future) to feed the learned-preferences
tracker ("consistently low-protein plans → bump protein emphasis
in the planner prompt"). This spec emits the events; the consumer
is out of scope.

### 4.10 UI changes

- **Daily totals band** above each day's recipe cards:
  `Mon: 2,140 kcal / 135 g P / 210 g C / 78 g F` plus a colored
  badge: green (in-band), yellow (band-miss), red (cap-breach).
  Clicking expands to show per-nutrient variance vs. target.
- **"Adjusted" badge** on any recipe that was swapped during
  repair, with a small "why?" tooltip: *"We replaced a low-protein
  dinner with this higher-protein option to meet your daily
  target."* Transparency is the point.
- **Breaches-remaining banner** at the top of the plan if any
  unresolved breach after repair: *"Your plan is 15 g short on
  protein today. Try the swap button on any dinner to adjust."*
- **Swap button** on every recipe card (ADR-005 expands the UX).

### 4.11 Observability

OTel counters:

- `nutrition.rollup.computed{cohort}`.
- `nutrition.rollup.breaches{nutrient, scope}`.
- `nutrition.repair.attempt{outcome}`.
- `nutrition.repair.iteration_count` histogram.
- `nutrition.repair.breaches_remaining` histogram.
- `nutrition.recipe_cache.{hit, miss}`.
- `nutrition.swap.endpoint{origin}` — origin ∈ `repair | user | substitution`.

Alerting: `breaches_remaining > 2` for >10% of plans in a rolling
hour → investigate (prompt drift, tolerance too tight, or KB gap).

### 4.12 Priority-grouped work items

| # | Item | Priority |
|---|------|----------|
| W1 | `tolerances/` module with `defaults.yaml`, loader, merge logic, tests | P0 |
| W2 | Migration `006_plan_rollup_and_repair.sql` + schema registration | P0 |
| W3 | Orchestrator: integrate rollup post-guardrail + persist snapshot | P0 |
| W4 | Repair ranking (§4.3) + targeted-swap helper | P0 |
| W5 | `meal_planning_agent.regenerate_with_constraint` + structured-output schema | P0 |
| W6 | Repair loop (concurrent intra-iteration + bounded iterations) | P0 |
| W7 | `MealPlanResponse` fields (`rollup`, `repair_history`, `breaches_remaining`, versions) | P0 |
| W8 | Recipe nutrient cache read/write wrapper around SPEC-009's `compute_recipe_nutrients` | P1 |
| W9 | `/swap` endpoint + `SwapConstraint` shape | P0 |
| W10 | `/rollup` endpoint + `/rerepair` endpoint (rate-limited) | P1 |
| W11 | Repair event emission | P1 |
| W12 | UI: daily totals + variance badge | FE | P1 |
| W13 | UI: "Adjusted" badge + tooltip; breaches-remaining banner | FE | P1 |
| W14 | Async-path fallback (high repair pressure → job) | P1 |
| W15 | Benchmarks: sync `/plan/meals` p99 ≤ 6 s, `/swap` p99 ≤ 4 s | P2 |

---

## 5. Rollout Plan

Feature flag `NUTRITION_ROLLUP_AND_REPAIR` (off → today's behavior,
on → SPEC-010 pipeline). Flag lives in unified config.

### Phase 0 — Foundation (P0)
- [ ] SPEC-008 and SPEC-009 frozen at 1.0.0.
- [ ] W1, W2 landed. Migration in staging. No behavior change.

### Phase 1 — Shadow rollup (P0)
- [ ] W3 (rollup only, no repair) behind flag.
- [ ] Shadow mode: for flag-off users, compute rollup in a
      background task after the plan ships; store snapshots;
      review tolerance breach rates for a week.
- [ ] Goal: understand how far off today's plans are. Calibration
      input for `defaults.yaml` before we start repairing.

### Phase 2 — Repair behind flag (P0)
- [ ] W4–W7, W9 landed behind flag.
- [ ] Flag on for internal team profiles.
- [ ] Monitor: `repair.iteration_count`, `breaches_remaining`,
      swap LLM latency, user-visible plan stability.

### Phase 3 — UI + ramp (P1)
- [ ] W10, W12, W13 shipped.
- [ ] Clinical reviewer signs off on `defaults.yaml` v1 values.
- [ ] 10% → 25% → 50% → 100% over three weeks. At each step:
      - Watch p99 plan latency.
      - Watch `breaches_remaining` distribution.
      - Watch whether users regenerate plans more or less (proxy
        for satisfaction).

### Phase 4 — Cleanup (P1/P2)
- [ ] W8 recipe cache live and measured.
- [ ] W11 repair events emitted.
- [ ] W14 async fallback live.
- [ ] W15 benchmarks baselined.
- [ ] Flag default on; flag-removal scheduled.

### Rollback
- Flag off → legacy path; rollup/repair skipped; DB tables inert.
- Migration is additive; no rollback needed.
- Stored snapshots retained for audit; no user data destroyed.

---

## 6. Verification

### 6.1 Unit tests

- `test_tolerances_merge.py` — defaults + clinical + profile
  merge in the right order; profile wins; clinical wins over
  defaults.
- `test_repair_ranking.py` — breach ranking + recipe selection
  prefer low-confidence and not-yet-liked recipes; tie-break by
  `suggested_date`.
- `test_regenerate_with_constraint.py` — prompt contains the
  specific numeric requirements; structured-output rejected if
  schema violates.

### 6.2 Integration tests

- `test_plan_meals_rollup_only.py` — flag on, no breaches:
  response contains rollup; repair_history empty; versions
  populated.
- `test_plan_meals_repair_success.py` — known protein-deficient
  LLM fixture triggers one repair iteration; replacement lifts
  protein into band; breaches_remaining empty.
- `test_plan_meals_repair_exhausted.py` — scenario where no swap
  satisfies all constraints after MAX_REPAIR_ITERS; response
  contains breaches_remaining with clear reasons; no exception.
- `test_swap_endpoint.py` — `POST /swap` with a
  `SwapConstraint{out_of: [greek_yogurt]}` returns a new recipe
  that passes guardrail + reports delta; rollup snapshot updated.
- `test_swap_guardrail_still_enforces.py` — swap that violates
  allergen guardrail is rejected; original recipe preserved;
  repair attempt recorded as `guardrail_rejected`.
- `test_rollup_cache_key_stable.py` — identical recipes across
  plans produce the same recipe-cache key; version bump
  invalidates.
- `test_high_repair_pressure_dispatches_async.py` — synthetic plan
  with 6+ breaches dispatches to async; sync endpoint returns a
  job id.

### 6.3 Golden plan tests

`tests/golden/plans/` — 10 end-to-end plan scenarios:

- Balanced 7-day plan → no repairs, clean rollup.
- Protein-deficient plan → one repair, rollup recovers.
- Sodium over-cap plan → two repairs needed, one succeeds, one
  exhausted (sodium-tight allergens prevent full fix); expected
  `breaches_remaining` present.
- T2D clinical per-meal carb cap breach → single-recipe swap.
- Plan fixture with a recipe the user rated 5★ previously → the
  repair loop does not swap it.

Each golden records the expected `repair_history` and
`breaches_remaining` shape.

### 6.4 Shadow-mode calibration (Phase 1)

Two weeks of shadow rollup data reviewed with the clinical
reviewer. Outcomes:

- Confirm `defaults.yaml` v1 tolerances produce a reasonable
  breach distribution (not "everything breaches" and not "nothing
  ever breaches").
- Adjust outlier bands before Phase 2.

### 6.5 Phase 3 ramp gates

- `repair.iteration_count` median ≤ 1 on typical plans.
- Sync-path p99 plan latency ≤ 6 s.
- `breaches_remaining` median ≤ 1 per plan after repair.
- User regeneration rate at 10% does not exceed pre-launch
  baseline by more than 20% (proxy for satisfaction).

### 6.6 Observability

- All §4.11 counters emit in staging and land on the dashboard.
- Repair events flow to the internal bus; ADR-004 consumer stubbed
  in this spec's phase-4 work.

### 6.7 Copy review

- "Adjusted" tooltip, breaches-remaining banner, and variance
  badges reviewed against the ADR-006 §6.5 copy checklist: neutral,
  informative, never shame-framed.

### 6.8 Cutover criteria

- All P0/P1 tests green.
- Phase 3 ramp completed with monitoring stable; no unresolved
  incident.
- Clinical reviewer sign-off on tolerances + shadow-mode
  calibration.
- Team lead + on-call approve promotion.

---

## 7. Open Questions

- **Tolerance defaults.** `defaults.yaml` v1 values are educated
  starting points. Phase 1 shadow data will recalibrate them.
  Expect one minor version bump on tolerances before Phase 3 ramp.
- **Swap selection when multiple breaches are tied.** Current
  ranking has a tie-break rule (later date first); alternative is
  user-configurable. v1 ships with the fixed rule; we revisit if
  Phase 3 data shows frustration ("why did you swap my Friday
  dinner instead of Monday lunch?").
- **"Liked meals are preserved" rule vs. safety.** If the only
  recipe contributing to a sodium breach is one the user rated
  5★, we currently skip it and surface the breach unresolved.
  Alternative is to swap it anyway and flag the swap. v1 preserves;
  users can manually override via `/swap`.
- **Tolerance overrides UI.** We deliberately do not ship a UI for
  per-profile tolerance overrides in v1. Advanced users and
  clinician workflows are a separate spec when a consumer appears.
- **Repair loop for async plans.** Currently the async path runs
  the same repair loop. High-latency swap LLM calls can make async
  plans take minutes. Acceptable for v1; a future optimization
  could parallelize across iterations if the dependency graph
  allows.
