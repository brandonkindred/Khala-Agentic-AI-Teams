# ADR-006 — Capstone: goal-progress and adherence dashboard

- **Status**: Proposed
- **Date**: 2026-04-17
- **Owner**: Nutrition & Meal Planning team
- **Related**: ADR-001 (deterministic targets), ADR-003 (nutrient rollup), ADR-004 (learned preferences), ADR-005 (workflow layer — cook-mode `cooked` event). Capstone for the nutrition roadmap.

## Context

After ADR-001 through ADR-005 land, the team owns four pieces of data
it does not currently connect:

1. **Prescribed targets** (ADR-001) — calories, macros, micros,
   clinical clamps derived from biometrics and conditions.
2. **Served plans with rollups** (ADR-003) — what we recommended and
   its nutrient totals per day and week.
3. **Cooking events** (ADR-005) — what the user actually made, with
   `status ∈ {made, partial, skipped, swapped}`, servings, and
   substitution deltas.
4. **Learned preferences** (ADR-004) — a side-channel, useful for
   explaining variance but not part of the outcome ledger.

What is missing is the ledger that ties these together with **observed
biometric outcomes** (weight, waist, resting HR, BP, fasting glucose,
A1c, lipid panels) and produces the single question every user
actually has: *"Is this working?"*

Three forces push us to build this now and not later:

1. **Honest goal claims require the full chain.** We currently imply
   that following recommendations helps the user reach their goal. We
   cannot show that, defend it, or correct it without this dashboard.
2. **Adherence and outcome are routinely conflated.** Users blame the
   plan when they did not actually eat the plan; users credit the plan
   when weather, sleep, or a different variable drove the change.
   Neither learning loop works if we cannot separate these.
3. **Safety.** Weight-trend dashboards are not neutral artifacts. For
   users with disordered-eating history, rapid weight loss, or
   clinical conditions, what we display, how often, and how we frame
   it affects health outcomes — not just product engagement. This
   cannot be bolted on.

## Decision

Add a **goal-progress ledger** and a **dashboard API + UI** that tie
prescribed → served → cooked → observed, with explicit adherence
decomposition and explicit safety rails.

### 1. The three adherence questions, made separable

Every goal-progress computation answers three independent questions,
never conflated:

- **Plan adherence** — did the user cook (or eat) what was planned?
  Derived from ADR-005 cooking events. Computed per day and week as
  `made_or_partial / (made + partial + skipped + swapped)`, with
  `swapped` handled by nutrient-delta weighting rather than binary
  credit.
- **Target adherence** — did what the user *actually ate* hit the
  daily/weekly targets from ADR-001? Derived from the **eaten
  rollup** (§3) against target tolerances (ADR-003 thresholds).
- **Goal progress** — is the observed biometric outcome moving toward
  the user's goal at the expected rate? Derived from biometric
  observations (§2) against the calculator's expected trajectory
  from ADR-001.

These are surfaced as three separate gauges, not rolled into a single
"score". A single score is the fastest way to mislead the user about
which lever to pull.

### 2. Biometric observations

New store `biometric_observations(client_id, kind, value, unit,
observed_at, source)`:

- `kind ∈ {weight_kg, waist_cm, hip_cm, body_fat_pct,
  resting_hr_bpm, bp_systolic, bp_diastolic, fasting_glucose_mgdl,
  a1c_pct, ldl_mgdl, hdl_mgdl, triglycerides_mgdl, ...}`.
- `source ∈ {manual, apple_health, google_fit, withings, fitbit,
  lab_upload}` — integrations handled by the existing Integrations
  team (CLAUDE.md notes the shared Google login and Playwright
  session infra).
- Entry endpoints:
  `POST /biometrics/{client_id}/observations` (single or batch),
  `GET /biometrics/{client_id}/observations?kind=...&since=...`,
  `DELETE /biometrics/{client_id}/observations/{id}`.
- Import adapters for the four device integrations are out of scope
  for v1 of this ADR — manual entry plus a generic `POST` endpoint is
  enough to ship the dashboard. Integrations ride on top without
  schema changes.

### 3. The eaten rollup

`nutrient_rollup/` (ADR-003) gains `rollup_eaten(client_id, period)`:

- Built from ADR-005 cooking events, not from the plan.
- `made` → full nutrients from the recipe rollup.
- `partial` → `servings_made / portions_servings` of the nutrients.
- `swapped` → the substituted recipe's rollup (computed in ADR-005
  when the substitution is recorded).
- `skipped` → zero for that recipe, **not** replaced by assumed
  intake. If the user's actual intake matters (it often does), they
  log it in §4 below.
- The eaten rollup is what `target_adherence` compares against.

### 4. Off-plan intake logging (lightweight)

Users eat things we did not plan. Without a way to log those, target
adherence is systematically wrong in one direction.

- New endpoint `POST /intake/{client_id}/quick-log` with a short
  `{description, meal_type, approx_portion, occurred_at}` body.
- Resolved with the ADR-002 parser + a small LLM pass to a recipe-like
  shape with estimated nutrients and a `confidence` score.
- Explicitly marked `off_plan=true` in the eaten rollup. UI shows them
  separately so plan adherence is not penalized for eating a lunch we
  never planned.
- Photo-based logging is deferred (v2). Text quick-log is enough to
  make adherence numbers honest.

### 5. Expected-trajectory model

The ADR-001 calculator already knows the user's goal (`goal_type`,
`target_weight_kg`, `rate_kg_per_week`) and the energy math. Extend it
with `compute_expected_trajectory(profile) -> Trajectory`:

- `Trajectory.series: list[(date, expected_value, confidence_band)]`
  for each relevant biometric kind.
- Grounded in the familiar identities (≈7,700 kcal per kg of fat mass
  on average, plus an early-phase water/glycogen correction, plus a
  drift term for metabolic adaptation that grows with cumulative
  deficit). These are rough but defensible; we show the band, not a
  point.
- Clinical-cohort trajectories (pregnancy weight gain by trimester,
  lactation, post-op) are explicitly out of scope and return
  `trajectory=None` with a "work with your clinician" note. We do
  **not** guess.

Observed biometrics are compared to the trajectory band. A run of
points outside the band triggers either a recalibration suggestion
(§7) or a safety rail (§6).

### 6. Safety rails

Non-negotiable behaviors, encoded in code and test:

- **Rate-of-loss floor.** If observed weight loss exceeds 1% body
  weight per week averaged over two consecutive weeks, the dashboard
  surfaces a caution and the next plan generation (ADR-001) clamps
  the goal delta, regardless of user preference. Repeated override
  requires explicit acknowledgment.
- **Underweight / BMI floor.** If BMI drops below 18.5 (or a
  user-specific clinician-set floor), weight-loss goals are disabled
  at the calculator level and the dashboard replaces progress gauges
  with a "revisit your goal" prompt.
- **Eating-disorder history flag** on the profile disables scale-
  centric views entirely and shows behavior-centric metrics only
  (plan adherence, variety, cook streak). Calorie numbers and weight
  trends can be hidden at the profile level.
- **Minors** (`age_years < 18`): growth-chart path only; no
  weight-loss trajectory, no deficit framing, no macro gauges by
  default. We route to "general guidance" as in ADR-001 §5.
- **Observation frequency.** The dashboard nudges at most **weekly**
  for weight and opt-in for the rest. We actively resist daily-weigh
  UX patterns — they are anxiety generators and statistically worse
  signal than weekly moving averages.
- **Language.** Copy is behavior- and energy-framed, never shame-
  framed. A copy-review checklist lives alongside the dashboard UI
  spec; dashboard strings are reviewed on change.

These rails are not negotiable per-user toggles; they are team
invariants. Individual tolerances (e.g. a clinician-raised BMI floor)
are set on the profile by authorized paths.

### 7. Recalibration loop

When goal progress persistently diverges from trajectory (e.g.,
observed deficit-equivalent ≠ prescribed deficit for 3+ weeks after
controlling for plan and target adherence), the dashboard offers a
**recalibration**:

- Recompute TDEE from observed outcome (`ΔE_observed ≈
  intake_eaten − ΔMass · 7700`), blended with the calculator's
  a-priori estimate as a Bayesian update.
- Surface the proposed new targets and show the user the reasoning
  ("your maintenance looks closer to 2,250 kcal than the 2,400 we
  estimated"). Never silently change targets.
- On accept, write a new `NutritionPlan` version with
  `calculator_version` from ADR-001 bumped and a `recalibration_id`
  tag for audit.

This is the payoff loop: the prescribed target becomes self-
correcting as real data accumulates. It also makes the system
honest about its own uncertainty.

### 8. Data model

New tables (registered via `shared_postgres.register_team_schemas`):

- `biometric_observations` (§2).
- `off_plan_intakes` (§4).
- `trajectory_snapshots(client_id, computed_at, profile_version,
  expected_series_json)` — so the dashboard can render the trajectory
  that was in force at a given week, not today's recomputed one.
- `adherence_snapshots(client_id, period_start, period_end,
  plan_adherence, target_adherence, goal_progress_status, inputs_hash)`
  — nightly job materializes these for fast reads; the dashboard
  never recomputes on the user's critical path.

Additive on existing models:

- `GoalsInfo.started_at`, `GoalsInfo.paused_at` — we need to know when
  a cycle began to frame progress honestly.
- `ClientProfile.ed_history_flag`, `clinician_overrides` — explicit
  flags for the safety rails in §6 so they are first-class, not
  hidden in `notes`.

### 9. API surface

- `GET /dashboard/{client_id}?period=week|month|quarter` — returns a
  `DashboardView`:
  - `plan_adherence`, `target_adherence`, `goal_progress_status`
    (three gauges, each with a confidence band and a short text
    explanation).
  - `trajectory`: the expected-vs-observed chart series.
  - `eaten_rollup`: ADR-003 rollup computed on cooked + off-plan.
  - `drivers`: short structured list ("Target protein was 132 g/day;
    you averaged 108 g. Three skipped lunches account for 18 g/day.").
  - `recalibration_suggestion?`: present when §7 fires.
  - `safety_state`: `ok | caution | rail_active`, with copy.
- `POST /dashboard/{client_id}/recalibrate` — accept/decline the
  recalibration.
- `GET /dashboard/{client_id}/drivers` — structured adherence
  decomposition, used by the UI to build "why" explanations and by
  the planner (ADR-004) to weight future recommendations.

The `drivers` field is deliberately structured (not free text); it is
the causal-explanation layer that prevents "score theater."

### 10. Interaction with the planner (ADR-004)

The dashboard is not just read-only. Its structured signals feed back
into the planner:

- Low plan adherence driven by skipped weeknight dinners →
  `effort_tolerance` downward on weekdays (ADR-004 signal).
- Swaps clustering around a specific ingredient → `ingredient_affinity`
  negative (ADR-004 signal).
- Target adherence failing only on fiber/micros → planner prompt adds
  an explicit fiber/micronutrient emphasis on next generation.

These feedbacks are deterministic transforms; the LLM is not asked to
"look at the dashboard and learn." The dashboard *is* the learning
signal in structured form.

## Consequences

### Positive

- The team can finally answer "is this working?" with real inputs and
  defensible reasoning, instead of vibes.
- Adherence decomposition makes the product's feedback actionable:
  users stop blaming the plan for unplanned eating, and we stop
  blaming the user for a mis-prescribed target.
- Recalibration (§7) makes the calculator self-correcting, which is
  how nutrition products build long-term trust over the 6–12 month
  timescales that actually matter.
- Safety rails are encoded in code, reviewable, and testable. This is
  the posture a consumer-health product has to take — "we intended to
  avoid harm" is not a control.
- Capstones the roadmap: every prior ADR gains a consumer — targets
  get validated, rollups become the adherence input, cooking events
  become the outcome ledger, preferences get ground-truth feedback.

### Negative / costs

- **Biggest surface area in the roadmap.** New stores, new endpoints,
  a UI with real design and copy requirements, safety-review burden,
  and integrations-team coordination for device ingestion.
  Mitigation: manual observation entry is enough to ship; device
  adapters are v2.
- **Safety-review ownership is a commitment.** Copy review,
  threshold review, and clinical-cohort handling need a named owner
  and a cadence. Without one, the rails drift. This must be called
  out at the planning stage, not discovered later.
- **Data freshness vs. cost tradeoff.** Nightly `adherence_snapshots`
  keep the dashboard fast but show a day-old picture. On-demand
  recomputation on request is available but rate-limited; we
  explicitly accept a one-day lag for the default view. Users who
  need real-time are a minority and can be served by a "refresh now"
  action with a visible wait.
- **Off-plan logging is a UX hole if left optional.** Adherence
  numbers are meaningfully wrong without it, and logging fatigue is
  real. Mitigation: text quick-log (§4) is deliberately <10s; we do
  not require full nutrient breakdowns from the user — we estimate,
  with a confidence band.
- **Recalibration introduces volatility.** Targets that move are
  disorienting. We surface changes explicitly, require user accept,
  and rate-limit recalibration suggestions to at most one per 21
  days, tied to accumulated data.
- **Dashboard is the highest-stakes UI the team owns.** It is where a
  shame-frame or a noisy chart does real harm. Non-trivial design
  investment is required; this ADR names the constraints (§6,
  "drivers" structure) but the design itself is a separate spec.

### Neutral / follow-ups

- Device integration adapters (Apple Health, Google Fit, Withings,
  Fitbit) are follow-up specs under the Integrations team, riding on
  the observation schema here unchanged.
- Photo-based quick-log (§4 v2) is its own spec.
- Cross-team integration: the Personal Assistant and Deepthought
  teams both hit the same "weekly reflection" pattern; a future ADR
  can factor the adherence-snapshot + trajectory machinery into a
  shared component. Not in scope here.
- A clinician-facing export (PDF/CSV of adherence snapshots and
  observation series for a patient's dietitian or PCP) is a natural
  v1.1 feature and uses no new data — it is a view over the existing
  ledger.
- This ADR formally closes the nutrition roadmap as originally
  scoped. Any item beyond this one should earn its own planning
  round.
