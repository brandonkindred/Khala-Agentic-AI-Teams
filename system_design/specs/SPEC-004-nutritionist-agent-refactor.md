# SPEC-004: Nutritionist agent refactor — deterministic calculator + narrator

| Field       | Value                                                    |
|-------------|----------------------------------------------------------|
| **Status**  | Proposed                                                 |
| **Author**  | Nutrition & Meal Planning team                           |
| **Created** | 2026-04-17                                               |
| **Priority**| P0 (capstone for ADR-001; unblocks ADR-003 and ADR-006)  |
| **Scope**   | `backend/agents/nutrition_meal_planning_team/agents/nutritionist_agent/`, `orchestrator/`, `shared/nutrition_plan_store`, `models.py` (additive), chat agent, `user-interface/` plan-rendering component |
| **Depends on** | SPEC-002 (profile fields), SPEC-003 (calculator) |
| **Implements** | ADR-001 §3 (narrator), §4 (caching + versioning), §5 (safety routing — agent side) |

---

## 1. Problem Statement

After SPEC-002 and SPEC-003 land we have two things the Nutritionist
agent does not yet use: a profile rich enough to drive equations, and
a pure-Python calculator that owns the numbers. This spec wires them
together.

The Nutritionist agent today (`agents/nutritionist_agent/agent.py`)
hands the profile to an LLM, asks for `DailyTargets` plus narrative,
regex-strips fences, and hopes. After this spec, the agent:

1. Calls `compute_daily_targets(profile)` first.
2. Routes unsupported cohorts to a general-guidance response before
   any LLM call.
3. Calls the LLM only to author `balance_guidelines`,
   `foods_to_emphasize`, `foods_to_avoid`, `notes`, and a friendly
   summary — never numbers.
4. Parses via the `llm_service` structured-output contract.
5. Caches with calculator-version awareness.

This is the user-visible payoff of ADR-001: reproducible, defensible,
calculator-anchored nutrition plans.

---

## 2. Current State

### 2.1 Today's flow

```mermaid
sequenceDiagram
    participant API
    participant Orch as Orchestrator
    participant Store as NutritionPlanStore
    participant NA as NutritionistAgent
    participant LLM

    API->>Orch: get_nutrition_plan(req)
    Orch->>Store: get_cached_plan(client_id, profile)
    alt cache hit (profile-hash)
        Store-->>Orch: NutritionPlan
    else miss
        Orch->>NA: run(profile)
        NA->>LLM: profile → "produce plan JSON"
        LLM-->>NA: free-form JSON (numbers + narrative)
        NA->>NA: regex-strip fences; json.loads
        NA-->>Orch: NutritionPlan (on fail: empty plan)
        Orch->>Store: save_plan(...)
    end
    Orch-->>API: NutritionPlanResponse
```

Relevant code:

- Orchestrator cache check: [orchestrator/agent.py:73-80](backend/agents/nutrition_meal_planning_team/orchestrator/agent.py:73)
- Agent: [agents/nutritionist_agent/agent.py:29-56](backend/agents/nutrition_meal_planning_team/agents/nutritionist_agent/agent.py:29)
- Chat agent also calls `nutritionist_agent.run` in `_handle_generate_nutrition_plan` ([orchestrator/agent.py:209-218](backend/agents/nutrition_meal_planning_team/orchestrator/agent.py:209))
- Cache key is a hash of the full profile; it invalidates on any profile change but knows nothing about calculator version.

### 2.2 Problems

1. Numbers are LLM-authored — the entire ADR-001 thesis.
2. Cohort routing (minors, pregnancy, ED history, unsupported
   clinical states) does not exist; any profile reaches the LLM.
3. Parsing is ad-hoc regex + json.loads; malformed output yields an
   empty plan silently.
4. Cache does not invalidate on calculator-version change; we will
   ship a calculator update and users will continue seeing stale
   plans until their profile happens to change.
5. Chat path duplicates the non-cached direct call — two call sites
   to update whenever the flow changes.

---

## 3. Goals and Non-Goals

### 3.1 Goals

- Numbers come from SPEC-003; narrative comes from the LLM.
- Cohort routing happens before any LLM call; unsupported cohorts
  produce a structured "general guidance" response with a
  "consult your clinician" note — no fabricated numbers.
- Parsing via `llm_service` structured-output contract (PR #184);
  no regex fence stripping in this module.
- Cache invalidates on `calculator_version` change **and** on
  biometric/clinical-field changes that affect calculator inputs.
- Single agent call site: chat and direct API go through the same
  helper in the orchestrator.
- API response includes a `rationale` and `calculator_version` so
  the UI can render the "why these numbers?" panel.

### 3.2 Non-goals

- **No meal planning changes.** Meal planner still reads the
  nutrition plan. ADR-003's rollup is a separate spec.
- **No preference learning.** ADR-004 is separate.
- **No new LLM provider plumbing.** We use whatever `llm_service`
  exposes; structured-output support is already present per PR #184.
- **No UI redesign.** This spec adds fields; the plan screen gets a
  rationale panel and a cohort banner, but the primary layout is
  unchanged.

---

## 4. Detailed Design

### 4.1 New flow

```mermaid
sequenceDiagram
    participant API
    participant Orch as Orchestrator
    participant Store as NutritionPlanStore
    participant Calc as nutrition_calc
    participant NA as NutritionistAgent
    participant LLM

    API->>Orch: get_nutrition_plan(req)
    Orch->>Store: get_cached_plan(client_id, profile, CALCULATOR_VERSION)
    alt cache hit
        Store-->>Orch: NutritionPlan (with rationale)
    else miss
        Orch->>Calc: compute_daily_targets(profile)
        alt UnsupportedCohortError
            Calc-->>Orch: cohort, guidance_key
            Orch->>NA: narrate_general_guidance(profile, guidance_key)
            NA->>LLM: structured-output call (narrative only)
            LLM-->>NA: GuidanceOnlyPayload
            NA-->>Orch: NutritionPlan (no numeric targets; cohort flag set)
        else OK
            Calc-->>Orch: CalculatorResult (targets, rationale, intermediates)
            Orch->>NA: narrate_plan(profile, targets, rationale)
            NA->>LLM: structured-output call (narrative only)
            LLM-->>NA: NarrativePayload
            NA-->>Orch: NutritionPlan (targets from Calc, narrative from LLM)
        end
        Orch->>Store: save_plan(profile, plan, calculator_version)
    end
    Orch-->>API: NutritionPlanResponse (includes rationale & cohort)
```

### 4.2 `models.py` additions (additive)

```python
class PlanCohort(str, Enum):
    general_adult = "general_adult"
    general_adult_sex_unspecified = "general_adult_sex_unspecified"
    pregnancy_lactation = "pregnancy_lactation"
    ed_adjacent = "ed_adjacent"
    clinician_guided = "clinician_guided"   # unsupported — guidance only
    minor = "minor"                         # unsupported — guidance only

class NutritionPlan(BaseModel):
    # ... existing ...
    rationale: Optional[Rationale] = None
    calculator_version: Optional[str] = None
    cohort: PlanCohort = PlanCohort.general_adult
    is_guidance_only: bool = False
    clinician_note: Optional[str] = None    # shown when guidance_only
    intermediates: Dict[str, float] = {}    # BMR, TDEE for UI "why?"
```

`Rationale` is re-exported from `nutrition_calc` (it is a plain
frozen dataclass; Pydantic's `ConfigDict(arbitrary_types_allowed=True)`
or an `__get_pydantic_core_schema__` adapter — pick at implementation
time, document the choice in the PR).

### 4.3 Agent refactor

`agents/nutritionist_agent/agent.py`:

```python
class NutritionistAgent:
    def __init__(self, model, narrator_schema=NarrativePayload, ...):
        self._agent = Agent(model=model, system_prompt=NARRATOR_SYSTEM_PROMPT)
        self._guidance_agent = Agent(model=model, system_prompt=GUIDANCE_SYSTEM_PROMPT)

    def narrate_plan(
        self,
        profile: ClientProfile,
        targets: DailyTargets,
        rationale: Rationale,
    ) -> NarrativePayload: ...

    def narrate_general_guidance(
        self,
        profile: ClientProfile,
        guidance_key: str,         # e.g. "pregnancy", "ckd_stage_5", "minor"
    ) -> GuidanceOnlyPayload: ...
```

- Two agents, two prompts, two response schemas. Both use
  `llm_service` structured output; no regex parsing in this file.
- `NARRATOR_SYSTEM_PROMPT` explicitly forbids emitting or modifying
  numeric targets. Includes: *"You will be given exact numeric
  targets. You may reference them in prose but MUST NOT contradict
  them. You are not the source of the numbers."*
- `GUIDANCE_SYSTEM_PROMPT` explicitly forbids emitting any numeric
  target at all; it produces qualitative food-group guidance and a
  "please work with your clinician" note.
- On LLM failure, return a `NarrativePayload` with empty narrative
  strings — the numeric part of the plan still ships. A plan with
  real numbers and blank guidelines is much better than no plan.

The old `run(profile) -> NutritionPlan` method is removed. All
callers move to the orchestrator helper in §4.4.

### 4.4 Orchestrator helper

`orchestrator/agent.py`:

```python
def _build_nutrition_plan(self, profile: ClientProfile) -> NutritionPlan:
    try:
        calc = compute_daily_targets(profile)
    except UnsupportedCohortError as e:
        narrative = self.nutritionist_agent.narrate_general_guidance(
            profile, e.guidance_key
        )
        return NutritionPlan(
            daily_targets=DailyTargets(),
            balance_guidelines=narrative.balance_guidelines,
            foods_to_emphasize=narrative.foods_to_emphasize,
            foods_to_avoid=narrative.foods_to_avoid,
            notes=narrative.notes,
            rationale=None,
            calculator_version=CALCULATOR_VERSION,
            cohort=PlanCohort(e.cohort),
            is_guidance_only=True,
            clinician_note=e.clinician_note,
            generated_at=utcnow_iso(),
        )
    except InsufficientInputError as e:
        return NutritionPlan(
            # blank plan, notes=“we need your height/weight/age/sex to
            # compute personalized targets”, cohort=general_adult,
            # is_guidance_only=True, completeness_missing=e.fields
            ...
        )

    narrative = self.nutritionist_agent.narrate_plan(
        profile, calc.targets, calc.rationale
    )
    return NutritionPlan(
        daily_targets=calc.targets,
        balance_guidelines=narrative.balance_guidelines,
        foods_to_emphasize=narrative.foods_to_emphasize,
        foods_to_avoid=narrative.foods_to_avoid,
        notes=narrative.notes,
        rationale=calc.rationale,
        calculator_version=calc.calculator_version,
        cohort=PlanCohort(calc.cohort),
        intermediates=calc.intermediates,
        generated_at=utcnow_iso(),
    )
```

`_get_or_generate_nutrition_plan` becomes a thin wrapper that:

1. Calls `get_cached_plan(...)` with `CALCULATOR_VERSION`.
2. On miss, calls `_build_nutrition_plan` and saves.

Both `get_nutrition_plan` (API) and `_handle_generate_nutrition_plan`
(chat) route through this single helper. The chat agent stops calling
`nutritionist_agent.run` directly.

### 4.5 Cache key and versioning

`shared/nutrition_plan_store.py`:

- Cache key derived from `(client_id, calculator_version,
  profile_cache_vector)` where `profile_cache_vector` is a hash of
  **only the fields the calculator reads** (biometrics, activity,
  goals, clinical conditions, medications, reproductive state, ED
  flag, clinician overrides, dietary needs that affect macros).
  Preferences and household composition do not invalidate; they do
  not change targets.
- Key layout: `nutrition_plan_v2:{client_id}:{calculator_version}:{profile_cache_vector}`.
- Postgres row includes `calculator_version` and
  `profile_cache_vector` as indexed columns; lookup is by exact match
  on all three.

Migration `003_nutrition_plan_v2.sql`:

```sql
ALTER TABLE nutrition_plans
    ADD COLUMN calculator_version TEXT,
    ADD COLUMN profile_cache_vector TEXT,
    ADD COLUMN cohort TEXT,
    ADD COLUMN is_guidance_only BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN rationale JSONB,
    ADD COLUMN intermediates JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX ON nutrition_plans (client_id, calculator_version, profile_cache_vector);
```

Pre-existing cached plans lack `calculator_version`; they are treated
as misses and regenerated on first access. No back-migration; old
rows are retained for audit and removed on a 90-day TTL job.

### 4.6 Structured-output schemas

`llm_service` structured-output payloads (Pydantic models):

```python
class NarrativePayload(BaseModel):
    balance_guidelines: List[str]
    foods_to_emphasize: List[str]
    foods_to_avoid: List[str]
    notes: str
    summary: str                     # 1–2 sentences, user-facing

class GuidanceOnlyPayload(BaseModel):
    balance_guidelines: List[str]
    foods_to_emphasize: List[str]
    foods_to_avoid: List[str]
    notes: str
    clinician_note: str              # explicit "work with your clinician"
```

Strict schemas per PR #184 — the LLM cannot return extra fields.

### 4.7 API surface

Existing endpoints unchanged at the HTTP level. Response bodies gain
new fields (additive; existing consumers ignore them):

- `NutritionPlanResponse.plan` includes `rationale`,
  `calculator_version`, `cohort`, `is_guidance_only`, `clinician_note`,
  `intermediates`.
- `ChatResponse.nutrition_plan` (already optional) includes the same.

Two small additions:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/plan/nutrition/{client_id}/rationale` | Returns just the rationale + intermediates (for the "why?" panel without re-fetching the plan) |
| `POST` | `/plan/nutrition/{client_id}/regenerate` | Forces cache bypass; returns a freshly computed plan. Rate-limited. |

### 4.8 Safety rail integration

Rails live in the calculator (SPEC-003 §4.9) **and** in the
orchestrator as a belt-and-suspenders check:

- After building any plan (even guidance-only), the orchestrator
  re-asserts:
  - `age_years < 18` → `cohort == PlanCohort.minor` and
    `is_guidance_only == True` and `goals.goal_type != "lose_weight"`.
  - `ed_history_flag == True` → cohort is `ed_adjacent` or
    guidance-only, and no deficit was applied (rationale check).
  - `kcal_target` (if set) ≥ safety floor.
- Any violation raises `SafetyInvariantError` and is treated as a
  production incident: the plan is not returned, the user sees a
  graceful error, and we page. This should never fire — it is a
  defense-in-depth check against calculator bugs.

### 4.9 UI changes

Minor; primary plan layout unchanged.

- **Cohort banner**: shown when `is_guidance_only`; renders
  `clinician_note` and a muted styling for numeric sections (which
  are absent in guidance-only mode).
- **"Why these numbers?" panel**: collapsible, renders
  `rationale.steps` as a friendly list ("Baseline from Mifflin–St
  Jeor: 1,540 kcal → TDEE at moderate activity: 2,385 kcal → 500
  kcal deficit for 0.45 kg/wk loss → safety floor OK → final 1,885
  kcal").
- **Regenerate button** (§4.7) with a confirmation dialog and a
  visible loading state — calculator work is millisecond-scale but
  the LLM narrative call is not.

### 4.10 Priority-grouped work items

| # | Item | Priority |
|---|------|----------|
| W1 | `models.py` additions (PlanCohort, NutritionPlan fields) | P0 |
| W2 | `NarrativePayload`, `GuidanceOnlyPayload` schemas | P0 |
| W3 | `NutritionistAgent.narrate_plan` / `narrate_general_guidance` via `llm_service` structured output | P0 |
| W4 | Remove `NutritionistAgent.run`; update chat agent and orchestrator to use `_build_nutrition_plan` | P0 |
| W5 | `nutrition_plan_store` cache key and `profile_cache_vector` derivation | P0 |
| W6 | Migration `003_nutrition_plan_v2.sql` + schema registration | P0 |
| W7 | Safety-invariant orchestrator checks + `SafetyInvariantError` handling | P0 |
| W8 | Rationale/intermediates in API responses | P1 |
| W9 | `/plan/nutrition/{id}/rationale` and `/regenerate` endpoints | P1 |
| W10 | Cohort banner component | FE | P1 |
| W11 | "Why these numbers?" panel | FE | P1 |
| W12 | Regenerate UX (button + dialog + loading) | FE | P2 |
| W13 | Deprecation notice in CHANGELOG; mark `NutritionistAgent.run` removed | P1 |

---

## 5. Rollout Plan

Single feature flag `NUTRITION_CALC_NARRATOR` (off → legacy agent,
on → SPEC-004 flow). Flag lives in unified config.

### Phase 0 — Wiring (P0)
- [ ] SPEC-003 frozen at `CALCULATOR_VERSION=1.0.0`.
- [ ] W1, W2, W6 landed; migration applied in staging.
- [ ] No behavior change yet; flag off.

### Phase 1 — Agent behind flag (P0)
- [ ] W3–W5, W7 landed behind flag.
- [ ] Orchestrator routes through `_build_nutrition_plan` when flag on.
- [ ] Unit, integration, and safety-invariant tests green.

### Phase 2 — Dogfood + compare (P0/P1)
- [ ] Shadow mode: for flagged-on users, also compute the legacy
      output and store a side-by-side record for 7 days. Review
      deltas with the clinical reviewer from SPEC-003 §6.6.
- [ ] Flag on for internal team profiles.
- [ ] No unexpected guidance-only responses (i.e., no misclassified
      cohorts on internal profiles).

### Phase 3 — Gradual rollout (P1)
- [ ] W8–W11 landed (rationale/cohort UI).
- [ ] 10% → 50% → 100% ramp over 2 weeks.
- [ ] Regeneration rate, `guidance_only` rate, and plan-fetch error
      rate monitored against pre-launch baselines.

### Phase 4 — Cleanup (P1/P2)
- [ ] W12 regenerate UX.
- [ ] W13 remove legacy `NutritionistAgent.run` path and flag.
- [ ] CHANGELOG entry published.

### Rollback

- Flag off instantly reverts all users to the legacy agent path. The
  new cache rows are inert (legacy code path does not read them).
- Schema migration is additive; no rollback needed.
- Plans stored during the rollout remain valid for audit; new legacy
  plans do not carry `calculator_version` and will be regenerated the
  next time the flag is turned on.

---

## 6. Verification

### 6.1 Unit tests

- `test_nutritionist_agent_narrate.py` — structured-output happy
  path; LLM malformed output returns empty narrative; LLM error
  falls back to empty narrative with numeric targets intact.
- `test_guidance_agent.py` — `narrate_general_guidance` never emits
  numeric targets (schema enforces this; test confirms).
- `test_plan_store_cache_key.py` — `profile_cache_vector` is stable
  across irrelevant changes (preferences edited → same vector),
  invalidates on relevant changes (weight changes → different
  vector); `calculator_version` change alone invalidates.
- `test_orchestrator_safety_invariants.py` — simulate a calculator
  bug (monkeypatch `compute_daily_targets` to return a deficit for a
  minor) and assert `SafetyInvariantError` is raised and surfaced as
  a graceful API error.

### 6.2 Integration tests

- `test_plan_nutrition_api_v2.py` — `POST /plan/nutrition` returns
  rationale + cohort on supported cohorts; guidance-only payload on
  pregnancy/minor/CKD-5.
- `test_plan_cache.py` — repeated request hits cache; bumping
  `CALCULATOR_VERSION` causes miss; editing a preference does not
  cause miss; editing weight does.
- `test_regenerate.py` — `/regenerate` bypasses cache and writes a
  new row; rate limit fires on the Nth call within window.
- `test_chat_plan_parity.py` — chat path and direct API path return
  byte-equal `NutritionPlan` for the same profile.

### 6.3 Shadow-mode comparison (Phase 2)

- Scripted diff between legacy output and new output for ~200
  anonymized profiles.
- Expected shape of the diff, reviewed with clinical reviewer:
  - Numeric targets become stable (no drift across repeated calls)
    and sometimes move substantially (especially where the legacy
    output was fabricated).
  - Narrative style is similar but no longer contains numbers.
  - Guidance-only cohorts are correctly identified.
- Sign-off gate: reviewer agrees the new outputs are preferable on
  ≥80% of sampled profiles and neutral on the remainder.

### 6.4 Contract test for structured output

- `test_narrative_payload_schema.py` — schema round-trips; extra
  fields rejected; missing required fields rejected.

### 6.5 Observability

- OTel counters:
  - `nutrition.plan.generated{cohort, is_guidance_only}`
  - `nutrition.plan.cache_{hit,miss,version_miss}`
  - `nutrition.plan.narrate_llm_{ok,error}`
  - `nutrition.plan.safety_invariant_triggered` — MUST remain 0 in
    steady state; any non-zero value pages.
  - `nutrition.plan.regenerate_called{reason}` (`user` | `admin`)
- Traces: `build_nutrition_plan` root span with child spans for
  `compute_daily_targets`, `narrate_plan` / `narrate_general_guidance`,
  `store.save_plan`. Latency budgets: p99 ≤ 3s for the narrate call
  path, p99 ≤ 50ms for cache-hit reads.

### 6.6 Cutover criteria (flag on by default)

- All P0/P1 tests green on `main`.
- Shadow-mode comparison signed off (§6.3).
- Phase 3 ramp completed with:
  - Plan-fetch error rate within +0.5 pp of legacy baseline.
  - `safety_invariant_triggered` count at zero throughout ramp.
  - `guidance_only` classification confirmed correct on a 50-profile
    audit sample.
- Clinical reviewer, team lead, and on-call approve promotion.

---

## 7. Open Questions

- **What counts as a "relevant" profile field for `profile_cache_vector`?**
  The closed list is defined in §4.5 but will want review — overly
  aggressive caching risks stale plans; overly conservative caching
  wastes LLM calls. First cut is conservative (include anything the
  calculator reads or the narrator's prompt injects).
- **Should the narrator see the `rationale`?** Yes, in v1, so the
  narrative can reference reasoning ("because your activity level is
  moderate…"). The risk is the LLM being tempted to argue with the
  numbers. Mitigation is prompt + schema; we re-evaluate if the
  shadow-mode review turns up issues.
- **Where does `guidance_key` come from for `UnsupportedCohortError`?**
  SPEC-003 §4.9 implies a small closed enum. We freeze it alongside
  the calculator version, and the guidance prompt switches on it
  deterministically.
- **Chat-phase UX for guidance-only.** The chat agent currently
  offers "generate a nutrition plan" as an action; on
  `is_guidance_only` it should offer a different follow-up (e.g.,
  "talk about general eating patterns" instead of meal targets).
  Small chat-agent prompt tweak, tracked in the chat agent's own
  backlog.
