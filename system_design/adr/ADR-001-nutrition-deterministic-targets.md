# ADR-001 — Nutrition daily targets are computed deterministically, not authored by the LLM

- **Status**: Proposed
- **Date**: 2026-04-17
- **Owner**: Nutrition & Meal Planning team
- **Related**: `backend/agents/nutrition_meal_planning_team/`

## Context

The Nutritionist agent today (`agents/nutritionist_agent/agent.py`) hands the
full `ClientProfile` to an LLM and asks it to return `DailyTargets`
(`calories_kcal`, `protein_g`, `carbs_g`, `fat_g`, `fiber_g`, `sodium_mg`,
plus `other_nutrients`) together with narrative guidance
(`balance_guidelines`, `foods_to_emphasize`, `foods_to_avoid`, `notes`).

Three forces are colliding:

1. **The numbers are unanchored.** `ClientProfile` (`models.py`) does not
   carry the inputs a dietitian would need to derive energy and macro
   targets — there is no `sex`, `age_years`, `height_cm`, `weight_kg`, or
   activity level. The prompt's only hint ("0.8g protein per kg") has no
   kg to multiply by. Outputs are therefore plausible-sounding but
   effectively fabricated; two calls with the same profile can disagree
   by hundreds of kcal.

2. **Downstream code trusts them.** `MealPlanningAgent` (and any future
   nutrient-rollup step — see the roadmap item on quantitative meal-plan
   validation) is being built on top of `DailyTargets`. Every layer
   stacked on a fabricated baseline inherits its error.

3. **Clinical and liability exposure.** Users with medical conditions
   (CKD protein caps, hypertension sodium caps, T2D carb distribution,
   pregnancy/lactation energy adds) or medication interactions
   (warfarin↔vitamin K, MAOI↔tyramine, ACEi↔potassium, GLP-1↔high-fat)
   cannot safely be served LLM-authored numbers. Even outside medical
   cohorts, "this app told me to eat 1,400 kcal" is a claim we should
   only make when we can show our work.

Prior art in the codebase supports a deterministic approach: the
Investment team already separates computation (Strategy Lab, market
data) from LLM narration (Advisor/IPS), and the LLM Service has a
structured-output contract (PR #184) for the parse half of this
problem. The nutrition team has not yet adopted either pattern.

## Decision

We will split the Nutritionist agent into a **deterministic calculator**
and an **LLM narrator**, and extend `ClientProfile` with the biometric
and clinical fields the calculator requires.

### 1. Extend the profile (`models.py`)

Add a `BiometricInfo` block and clinical fields to `ClientProfile`:

- `sex: Literal["female","male","other","unspecified"]`
- `age_years: int | None`
- `height_cm: float | None`
- `weight_kg: float | None`
- `body_fat_pct: float | None` (optional; enables Katch–McArdle)
- `activity_level: Literal["sedentary","light","moderate","active","very_active"]`
  mapped to PAL multipliers (1.2 / 1.375 / 1.55 / 1.725 / 1.9)
- `medical_conditions: list[str]` (free-text tags, e.g. `ckd_stage_3`,
  `hypertension`, `t2_diabetes`, `pregnancy_t2`, `lactation`)
- `medications: list[str]`
- `target_weight_kg: float | None` and `rate_kg_per_week: float | None`
  for cut/bulk targets

Existing `GoalsInfo.goal_type` stays as the high-level intent
(`maintain`, `lose_weight`, `gain_weight`, `muscle`); the calculator
consumes both.

### 2. Deterministic calculator

Add `nutrition_meal_planning_team/nutrition_calc/` as a pure-Python
module with no LLM dependency:

- `bmr.py` — Mifflin–St Jeor as default; Katch–McArdle when
  `body_fat_pct` is present.
- `tdee.py` — `BMR × PAL` with documented multipliers.
- `energy_goal.py` — apply the goal delta (−500 kcal/day for 0.45 kg/wk
  loss, capped at a floor of `max(1200, 0.8×BMR)` for safety; symmetric
  surplus for gain).
- `macros.py` — protein by body weight (1.2–2.0 g/kg clamped by
  goal/condition), fat ≥ 20% of kcal, carbs = remainder; all clamped to
  AMDR bands (P 10–35%, F 20–35%, C 45–65%).
- `micros.py` — DRI lookup by sex/age/pregnancy-lactation for fiber,
  sodium cap, potassium, iron, calcium, vitamin D, B12; returns both a
  target and an upper limit where one exists.
- `clinical_overrides.py` — condition-specific clamps applied last
  (e.g. CKD stage 3: protein ≤ 0.8 g/kg; hypertension: sodium ≤ 1500 mg;
  pregnancy T2/T3: +340/+450 kcal; lactation: +330–400 kcal).
- `targets.py::compute_daily_targets(profile) -> (DailyTargets, Rationale)`
  — orchestrates the above, returning the numbers **and** a structured
  `Rationale` (which equations ran, which overrides applied, which
  inputs were defaulted). The rationale is what makes the output
  auditable.

All functions are unit-tested against published reference values
(Mifflin–St Jeor worked examples, DRI tables, AMDR bands).

### 3. LLM narrator

`NutritionistAgent.run` becomes:

1. `targets, rationale = compute_daily_targets(profile)`.
2. If any required input is missing, return a `NutritionPlan` with the
   computed fields populated where possible plus a `notes` field asking
   for the missing inputs — do **not** ask the LLM to fill numeric gaps.
3. Call the LLM with `(profile, targets, rationale)` and ask only for
   `balance_guidelines`, `foods_to_emphasize`, `foods_to_avoid`, and
   `notes`. The prompt explicitly forbids emitting or modifying numeric
   targets.
4. Use the `llm_service` structured-output contract (no regex
   markdown stripping) to parse.
5. Merge: narrative fields from the LLM, numeric fields from the
   calculator. The calculator wins on conflict.

### 4. Persistence and caching

- `NutritionPlan` gains `rationale: Rationale` (versioned; see below)
  and `calculator_version: str`.
- `nutrition_plan_store.get_cached_plan` invalidates when
  `calculator_version` changes **or** when any biometric /
  clinical-override-relevant field changes, not just when the full
  profile hash changes.

### 5. Safety rails

- Hard refuse (calculator raises) on biologically implausible inputs
  (age < 2 or > 120, BMI < 10 or > 80, kcal floor breach after goal
  delta).
- Pregnancy, lactation, eating-disorder history, or age < 18 route to a
  narrower "general guidance only" path with an explicit
  "consult your clinician" note; we do not emit aggressive deficits for
  these cohorts regardless of `goal_type`.
- Medication/condition interactions remain the responsibility of the
  allergen/interaction guardrail (separate ADR); this ADR only ensures
  the calculator's outputs reflect clinical clamps.

## Consequences

### Positive

- Daily targets become reproducible, testable, and explainable. We can
  show the user the equation, the inputs, and the overrides.
- Downstream work (nutrient rollup, adherence tracking, goal progress)
  has a stable, trustworthy baseline.
- Clinical cohorts are handled by code that can be reviewed and
  regression-tested, not by prompt suggestions.
- Cost and latency of the nutrition-plan path drop (the LLM is doing
  ~1/3 of the work it was; parsing is simpler).
- Opens the door to a "Why these numbers?" UI panel, which is a large
  trust win in consumer health.

### Negative / costs

- **Profile migration.** New required-ish fields on `ClientProfile`
  require a Postgres migration, an intake-flow change, and a backfill
  strategy for existing profiles (likely: mark plans stale, prompt the
  user for the missing inputs on next visit).
- **Intake UX gets heavier.** We are asking for height/weight/age/sex
  that we did not previously ask for. Mitigation: progressive
  disclosure — gate full numeric plans on having these, but keep the
  guideline/meal paths usable with partial profiles.
- **Clinical review burden.** Once we ship equations, they are our
  equations. We need a documented owner and a review cadence for
  `clinical_overrides.py` and `micros.py`; reference tables drift (DRIs
  are revised periodically).
- **Breaking change for API consumers** reading `NutritionPlan` — the
  added `rationale` and `calculator_version` fields are additive
  (non-breaking), but guideline-authoring clients that relied on the
  LLM's numeric outputs will see those numbers stabilize and sometimes
  change versus today's output. This is the intended behavior but
  should be called out in the CHANGELOG.
- **Scope creep risk.** `clinical_overrides.py` can grow indefinitely.
  We keep it to a short, cited allowlist of conditions in v1; anything
  beyond that returns a "consult your clinician" note rather than a
  half-implemented clamp.

### Neutral / follow-ups

- A subsequent ADR will cover the deterministic allergen /
  drug–nutrient interaction guardrail (item #2 on the nutrition-team
  roadmap), which sits on top of the profile additions made here.
- A follow-up spec will cover the nutrient-rollup / meal-plan
  validation loop (roadmap item #3), which depends on this ADR for its
  "expected daily targets" input.
- UI work to collect the new biometric fields and to render the
  `Rationale` panel is tracked separately under the nutrition UX
  workstream.
