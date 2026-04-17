# ADR-002 — Deterministic allergen, dietary, and drug–nutrient interaction guardrail for meal recommendations

- **Status**: Proposed
- **Date**: 2026-04-17
- **Owner**: Nutrition & Meal Planning team
- **Related**: ADR-001 (deterministic daily targets), `backend/agents/nutrition_meal_planning_team/`

## Context

Allergen, dietary, and drug-interaction enforcement today is purely
prompt-based. The meal-planning agent prompt
(`agents/meal_planning_agent/agent.py`) says "respect
`max_cooking_time_minutes`... avoid ones like past misses", and the
nutritionist prompt (`agents/nutritionist_agent/agent.py`) says
"respect allergies and dietary needs" — and that is the entire
mechanism. An LLM asked for a "nut-free dinner" will occasionally emit
almond flour, pesto (pine nuts), marzipan garnishes, or
Worcestershire (anchovy, for pescatarian-except-fish users). For a
warfarin patient it will cheerfully recommend a kale-and-spinach salad.

Three forces make this the highest-severity bug class on the team:

1. **Asymmetric cost.** A recommendation-quality miss costs one meal.
   An allergen miss can cost a life; a warfarin × vitamin-K miss can
   land a patient in the ED. The product cannot ship to any medical
   cohort — or arguably to any consumer — without a deterministic
   backstop.
2. **LLM behavior is probabilistic and drifts with model swaps.** We
   already support multiple providers (Ollama, Claude) via
   `llm_service`; each upgrade silently changes the prompt-adherence
   surface. A guardrail that depends on the current model's
   instruction-following is a guardrail that expires.
3. **The ingredient representation is ambiguous.** `MealRecommendation.
   ingredients: List[str]` is free text ("1 tbsp soy sauce", "a handful
   of cashews", "Parmesan"). Even a perfect prompt cannot make a
   downstream filter work if ingredients are not normalized.

We explicitly scope this ADR to the **enforcement** layer. The ADR-001
calculator clamps *numeric targets* for clinical cohorts; this ADR
ensures the *ingredients themselves* in every emitted recommendation
are legal for this client.

## Decision

Introduce a deterministic post-generation guardrail that runs between
the meal-planning agent and the orchestrator's recording step. Its
contract: **no `MealRecommendationWithId` is persisted or returned to
the API unless it has passed the guardrail.**

### 1. Canonical ingredient model

Add `nutrition_meal_planning_team/ingredient_kb/` containing:

- `canonical_foods.yaml` — seed list of ~2,000 common ingredients keyed
  by canonical id (`fdc_id` where available), each with:
  - `allergen_tags`: subset of the fixed taxonomy below.
  - `dietary_tags`: e.g. `animal`, `dairy`, `egg`, `honey`, `gluten`,
    `high_fodmap`, `nightshade`, `alcohol`.
  - `interaction_tags`: e.g. `vitamin_k_high`, `tyramine_high`,
    `potassium_high`, `grapefruit`, `licorice`, `st_johns_wort`.
  - `aliases`: free-text surface forms ("almond flour", "ground almonds",
    "amandes en poudre").
- `allergen_taxonomy.py` — fixed, closed enum: `peanut`, `tree_nut`,
  `dairy`, `egg`, `soy`, `wheat`, `gluten`, `fish`, `shellfish`,
  `sesame`, `mustard`, `celery`, `sulfites`, `lupin`, `mollusc`
  (FDA Big-9 + EU-14 superset).
- `parser.py::parse_ingredient(line: str) -> ParsedIngredient` —
  deterministic rule-based parser that extracts `(qty, unit, name)` and
  resolves `name` to a canonical id via aliases + fuzzy match. Unknown
  items return `canonical_id=None` with a `confidence` score.

### 2. Profile-side normalization

On profile save (orchestrator `update_profile`), resolve
`allergies_and_intolerances` and `dietary_needs` strings to:

- `resolved_allergen_tags: set[AllergenTag]`
- `resolved_dietary_tags: set[DietaryTag]` (e.g. `vegan` expands to
  `forbid: {animal, dairy, egg, honey}`)

Unresolved free-text stays on the profile as `unresolved_restrictions`
and is shown to the user for confirmation. Ambiguity surfaces to the
user; it does not silently pass.

### 3. Guardrail pipeline

Add `nutrition_meal_planning_team/guardrail/` with a single entry
point `check_recommendation(profile, rec) -> GuardrailResult`:

1. **Parse** each ingredient line with `parse_ingredient`.
2. **Allergen check** — any parsed `allergen_tags` ∩
   `profile.resolved_allergen_tags` → `reject(reason=allergen)`.
3. **Dietary check** — any `dietary_tags` violating active dietary
   tags → `reject(reason=dietary)`.
4. **Interaction check** — for each `medications[]` tag, look up
   contra-indicated `interaction_tags` in a curated
   `interactions.yaml` (warfarin↔`vitamin_k_high`, MAOI↔`tyramine_high`,
   ACEi/ARB + K-sparing diuretics↔`potassium_high`,
   statins/amiodarone↔`grapefruit`, SSRIs↔`st_johns_wort`,
   GLP-1↔`very_high_fat`). Hits → `flag` (not hard reject) with a
   required "consult your clinician" note, unless the profile opts into
   hard-reject for a given class.
5. **Unknown-ingredient policy** — any `canonical_id=None` with
   `confidence < threshold` → `reject(reason=unresolved_ingredient)`.
   We fail closed: if we cannot tell what it is, we do not serve it.
6. **Condition-specific caps** — e.g. CKD-3 caps *phosphorus* and
   *potassium* flagged ingredients per meal; hypertension flags
   >800 mg sodium per meal. These rely on per-ingredient nutrient data
   (shared with the nutrient-rollup work in the follow-up ADR).

Result is `{passed: bool, violations: [Violation], flags: [Flag]}`.

### 4. Orchestrator integration

`NutritionMealPlanningOrchestrator._record_suggestions` becomes a
two-pass pipeline:

1. Run guardrail on each suggestion.
2. For each `rejected` suggestion, call a **targeted regeneration**: a
   single-suggestion LLM call with the violation spelled out
   ("replace this dinner; it contains `pine_nut` which violates
   `tree_nut` allergy"). Accept up to `MAX_REGEN_RETRIES` (default 2).
3. Suggestions that still fail after retries are **dropped, not
   served**, and the response includes a `dropped_count` with
   structured reasons so the UI can explain the gap ("We filtered 1
   suggestion that conflicted with your nut allergy"). Trust is built
   by *visible* enforcement.
4. Flagged-but-not-rejected suggestions are served with the
   `clinical_flags: [Flag]` attached to `MealRecommendationWithId`;
   the UI renders them as a caution banner.

### 5. Observability

- Emit OTel metrics: `guardrail.rejections{reason}`,
  `guardrail.flags{class}`, `guardrail.unknown_ingredient_rate`,
  `guardrail.regen_retries`.
- Log every rejection with profile id, ingredient, and resolved tag.
  This is both a safety audit trail and the training signal for
  iterating on `canonical_foods.yaml`.

### 6. Versioning

`canonical_foods.yaml` and `interactions.yaml` carry a `version` field
persisted on every `MealRecommendation` record (`guardrail_version`).
On version bump we re-run the guardrail against the active meal plan
in the background and notify the user of any new rejections — relevant
when a medication is added after the plan is generated.

## Consequences

### Positive

- Allergen enforcement becomes a property of code, not of prompt
  adherence. Model swaps no longer risk user safety.
- Unknown ingredients fail closed; the failure mode is "missing
  recommendation", not "mystery recommendation".
- Creates the ingredient-resolution layer that the nutrient-rollup ADR
  and the substitution agent (ADR-005) both need; this is the
  foundational change that unlocks those.
- Generates structured rejection data, which is the fastest way to
  improve the prompt *and* the ingredient KB.
- Gives the UI something honest to display ("1 suggestion filtered
  for your shellfish allergy") — users perceive enforcement and trust
  the product more.

### Negative / costs

- **KB curation is ongoing work.** Seeding ~2,000 ingredients is a
  one-time lift; keeping aliases and tags correct is a long tail. We
  will accept gaps initially and lean on the `unresolved` rejection
  path plus logging to prioritize additions.
- **Latency.** Targeted regenerations add up to `MAX_REGEN_RETRIES`
  extra LLM calls per rejected suggestion. Expected worst case on a
  7-day plan with strict allergies: +3–5 LLM calls. Mitigation: retries
  are per-suggestion and can run concurrently.
- **Free-text ingredient strings get stricter.** The LLM must emit
  ingredients that parse; we will need prompt examples and a parse
  loop until adherence is high enough. The guardrail will reject many
  outputs from today's prompt until the prompt is tuned.
- **False positives on edge cases.** Worcestershire-as-pescatarian,
  gelatin-as-vegetarian, trace-dairy in processed foods — we will be
  conservative (reject) and rely on user-editable overrides to relax.
  Better a rejected valid meal than a served unsafe one.
- **Clinical interaction coverage is deliberately narrow.** v1 covers
  the listed med classes only; anything outside the allowlist is a
  "consult your clinician" flag, never a silent pass. Scope discipline
  here is load-bearing — this file must not become WebMD.

### Neutral / follow-ups

- Substitution agent (ADR-005) reuses the guardrail verbatim: a
  proposed swap is not emitted unless it passes the same check.
- Nutrient-rollup ADR (ADR-003) consumes the parsed, canonicalized
  ingredient list; no separate parse is needed.
- A separate UI spec covers the "filtered suggestions" explanation,
  the flag banner, the ambiguity-resolution dialog for
  `unresolved_restrictions`, and the override UX for edge cases.
