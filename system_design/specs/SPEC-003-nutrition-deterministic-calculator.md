# SPEC-003: Deterministic nutrition calculator (`nutrition_calc/`)

| Field       | Value                                                    |
|-------------|----------------------------------------------------------|
| **Status**  | Proposed                                                 |
| **Author**  | Nutrition & Meal Planning team                           |
| **Created** | 2026-04-17                                               |
| **Priority**| P0 (blocks SPEC-004, ADR-003, ADR-006)                   |
| **Scope**   | New pure-Python module `backend/agents/nutrition_meal_planning_team/nutrition_calc/` |
| **Depends on** | SPEC-002 (profile fields required as calculator inputs) |
| **Implements** | ADR-001 §2 (calculator), §3 (narrator contract — calculator side), §4 (caching — version constant) |

---

## 1. Problem Statement

Daily nutrient targets for a client (calories, macros, key micros,
plus condition-specific clamps) are today authored by an LLM with no
biometric inputs. Outputs are plausible-sounding but unanchored and
non-reproducible. ADR-001 calls for a deterministic calculator that
owns the numbers, with the LLM demoted to writing narrative around
them.

This spec defines that calculator as a standalone pure-Python module
with a narrow interface, exhaustive unit tests against published
reference values, and an explicit rationale output that lets us show
our work.

The module has no dependency on `Agent`, `llm_service`, or any store.
It is importable and testable without Postgres, without the Unified
API, and without a running LLM. That separation is deliberate — it is
what makes the numbers reviewable.

---

## 2. Current State

### 2.1 Numbers today

`agents/nutritionist_agent/agent.py:37-56`:

```python
prompt = (
    "Client profile:\n"
    + profile.model_dump_json(indent=2)
    + "\n\nProduce the nutrition plan JSON (daily_targets, ...)"
)
# LLM returns numbers; we regex-strip fences and hope.
```

There is no anchored equation, no cohort-specific clamp, no rationale
trail. Two calls with the same profile can disagree by hundreds of
kcal, and we have no defensible story for why any number was chosen.

### 2.2 What ADR-001 prescribes

- Mifflin–St Jeor BMR (default); Katch–McArdle when body fat present.
- TDEE = BMR × PAL with documented multipliers.
- Goal delta applied with a **kcal floor** (safety).
- Macros from AMDR bands with protein by body weight.
- Micros from DRI tables by sex/age/reproductive state.
- Clinical overrides applied last (CKD, HTN, pregnancy, lactation,
  etc.).
- A `Rationale` describing which equations ran and which overrides
  applied.

### 2.3 Gaps this spec fills

- No module boundary. No versioned outputs. No tests against
  reference values. No safe refusal for implausible or out-of-cohort
  inputs.

---

## 3. Goals and Non-Goals

### 3.1 Goals

- Ship a pure-Python module exposing one function:
  `compute_daily_targets(profile: ClientProfile) -> CalculatorResult`.
- Produce deterministic, reproducible outputs — same inputs, same
  outputs, byte-for-byte, forever on a given `CALCULATOR_VERSION`.
- Surface a structured `Rationale` that names each equation, each
  input it consumed (including defaults), and each clinical override
  applied — this is the audit trail and the UI "why these numbers?"
  payload.
- Fail closed on implausible inputs or cohorts outside our supported
  list; do not fall back to guesses.
- Expose a version constant other components can pin on for caching
  (SPEC-004 §4.4).

### 3.2 Non-goals

- **No agent integration.** SPEC-004 wires this into the
  Nutritionist agent. This spec ships a library.
- **No persistence.** Callers store results; the calculator is
  stateless.
- **No personalization from history.** The calculator consumes
  profile inputs only. Adaptive recalibration from observed outcomes
  is ADR-006's `recalibration_loop` and is out of scope here.
- **No meal-level logic.** Meal planning and nutrient rollup are
  ADR-003.

---

## 4. Detailed Design

### 4.1 Module layout

```
backend/agents/nutrition_meal_planning_team/nutrition_calc/
├── __init__.py            # exports compute_daily_targets, CALCULATOR_VERSION
├── version.py             # CALCULATOR_VERSION = "1.0.0"
├── bmr.py                 # Mifflin–St Jeor, Katch–McArdle
├── tdee.py                # PAL multipliers
├── energy_goal.py         # goal delta + safety floor
├── macros.py              # protein/fat/carb computation, AMDR bands
├── micros.py              # DRI tables by sex/age/repro state
├── clinical_overrides.py  # condition-specific clamps
├── tables/
│   ├── amdr.yaml
│   ├── dri.yaml           # versioned DRI tables
│   ├── pal.yaml
│   └── retention.yaml     # (reserved for SPEC-005 use)
├── rationale.py           # Rationale, Step dataclasses
├── errors.py              # CalculatorError, ImplausibleInputError, UnsupportedCohortError
└── targets.py             # compute_daily_targets orchestration
```

### 4.2 Public interface

```python
# nutrition_calc/__init__.py
from .version import CALCULATOR_VERSION
from .targets import compute_daily_targets
from .rationale import Rationale, RationaleStep
from .errors import (
    CalculatorError,
    ImplausibleInputError,
    UnsupportedCohortError,
    InsufficientInputError,
)

@dataclass(frozen=True)
class CalculatorResult:
    targets: DailyTargets             # from models.py (existing)
    rationale: Rationale
    calculator_version: str           # == CALCULATOR_VERSION
    cohort: str                       # e.g. "general_adult", "pregnant_t2"
    warnings: list[str] = field(default_factory=list)

def compute_daily_targets(profile: ClientProfile) -> CalculatorResult: ...
```

Contract:

- **Deterministic.** Same `profile` → same `CalculatorResult`. No
  clocks, no randomness, no I/O.
- **Pure.** No side effects. No logging at WARN or above — log at
  DEBUG with structured fields only.
- **Explicit failure.** Raises `InsufficientInputError` when a
  required field is missing (e.g. weight), `ImplausibleInputError`
  when a validator in SPEC-002 somehow was bypassed, and
  `UnsupportedCohortError` when the cohort routing (§4.9) does not
  support the profile.

### 4.3 BMR (`bmr.py`)

Default: **Mifflin–St Jeor** (preferred per ISSN position papers
over Harris–Benedict for adults).

```
female: 10·kg + 6.25·cm − 5·age − 161
male:   10·kg + 6.25·cm − 5·age + 5
```

When `body_fat_pct` is present, **Katch–McArdle**:

```
BMR = 370 + 21.6 · LBM_kg,   where LBM_kg = kg · (1 − body_fat_pct/100)
```

Katch–McArdle is preferred for users with accurate body composition
measurement; when `body_fat_pct` is present we use it and the
`Rationale` records the switch. `Sex.other` or `Sex.unspecified`
with body fat present → Katch–McArdle (sex-independent). Without
body fat, `Sex.other`/`unspecified` routes to the sex-averaged
Mifflin variant: `10·kg + 6.25·cm − 5·age − 78` (midpoint of −161
and +5); the `Rationale` notes the approximation and the cohort is
tagged `general_adult_sex_unspecified`.

### 4.4 TDEE (`tdee.py`)

`TDEE = BMR × PAL` where PAL is from `tables/pal.yaml`:

```
sedentary:    1.2
light:        1.375
moderate:     1.55
active:       1.725
very_active:  1.9
```

No other multipliers. No NEAT adjustments in v1 — that would require
inputs we do not collect (occupation, steps) and would complicate
the reproducibility story.

### 4.5 Energy goal (`energy_goal.py`)

Input: `TDEE`, `GoalsInfo`, `BiometricInfo`, `ClinicalInfo`.

```
kcal_target =
    TDEE                                    if goal_type == "maintain"
    TDEE − 7700·rate_kg_per_week/7          if goal_type == "lose_weight"
    TDEE + 7700·rate_kg_per_week/7          if goal_type == "gain_weight"
    TDEE + 250                              if goal_type == "muscle" and strength-training PAL, else +150
```

Then apply the **safety floor**:

```
floor = max(1200, 0.8 · BMR)
if kcal_target < floor:
    kcal_target = floor
    rationale.add("safety_floor_applied", from=kcal_target, to=floor)
```

`rate_kg_per_week` is clamped at input (SPEC-002) to ≤1.0; the
calculator additionally enforces a per-profile sanity cap of 1% of
body weight per week (rationale note if the input rate is reduced).

Pregnancy/lactation cohorts skip the deficit path entirely (see
§4.9).

### 4.6 Macros (`macros.py`)

Protein:

```
base_g_per_kg =
    1.2 if goal_type == "maintain" else
    1.6 if goal_type in ("lose_weight", "muscle") else
    1.4                                       # gain_weight default
protein_g = clamp(base_g_per_kg · kg, p_amdr_low, p_amdr_high)
```

Where `p_amdr_low/high` are AMDR bounds (10–35% of kcal).

Fat: `fat_g = max(kcal_target · 0.20, kcal_target · 0.25 if Keto, ...)` — dietary-need adjustments routed to a small table keyed by `dietary_needs` (keto, low-fat, etc.). Default 25% of kcal.

Carbs: remainder of the kcal budget after protein and fat, then
clamped to AMDR (45–65% of kcal). If the remainder lands outside
AMDR after protein and fat, protein is reduced *first* (down to
minimum 0.8 g/kg) before fat, and the `Rationale` records each
clamp.

AMDR constants live in `tables/amdr.yaml` with a version field; any
change bumps `CALCULATOR_VERSION`.

### 4.7 Micros (`micros.py`)

DRI table keyed by `(sex, age_band, reproductive_state)`. Returns a
`DailyTargets.other_nutrients` dict plus structured `MicroTarget`
records that the UI and ADR-003 consume.

V1 covers: fiber, sodium (upper limit), potassium, calcium, iron,
vitamin D, vitamin B12, phosphorus, vitamin K. DRI values are
verbatim from the 2020 DRI tables and are cited in `dri.yaml`
comments.

Each micro target carries:
```
{
    target: float,
    unit: str,
    lower_bound: Optional[float],
    upper_bound: Optional[float],   # UL for sodium, iron, etc.
    source: str,                    # "DRI 2020, Table X"
}
```

### 4.8 Clinical overrides (`clinical_overrides.py`)

Applied **last**. Each override is a pure function `(CalculatorResult)
-> CalculatorResult` that may:

- Lower a target (protein cap for CKD stages 3–5).
- Lower an upper bound (sodium ≤1500 mg for hypertension).
- Adjust kcal (pregnancy T2/T3: +340/+450 kcal; lactation: +330 (0–6 mo) or +400 (6–12 mo) kcal).
- Tighten carb distribution (T2D: per-meal carb cap — this is
  metadata, not a daily-target change; surfaced for ADR-003).
- Add rationale entries.

Overrides chain deterministically in a fixed order defined in
`clinical_overrides.py:ORDER`. If two overrides set the same cap,
the lower value wins and both are recorded.

v1 coverage matches SPEC-002's closed enum list. Out-of-enum
conditions are ignored by the calculator (the freetext list is
for the agent narrator only, not for numeric clamps).

### 4.9 Cohort routing (§4.9 of ADR-001 §5)

Before any computation, `targets.py` routes the profile to a cohort:

```mermaid
flowchart TD
    P[Profile] --> AGE{age_years < 18?}
    AGE -->|Yes| MIN[cohort=minor → UnsupportedCohortError]
    AGE -->|No| PREG{reproductive_state in<br/>{pregnant_t1..t3, lactating}?}
    PREG -->|Yes| PC[cohort=pregnancy_lactation<br/>skip deficit, apply trimester adjustments]
    PREG -->|No| ED{ed_history_flag?}
    ED -->|Yes| EDC[cohort=ed_adjacent<br/>skip deficit, kcal=TDEE, no macro minimums below DRI]
    ED -->|No| CLIN{condition requires<br/>clinician-guided only?<br/>e.g. ckd_stage_4..5}
    CLIN -->|Yes| CG[cohort=clinician_guided → UnsupportedCohortError]
    CLIN -->|No| GEN[cohort=general_adult]
    PC --> COMPUTE[compute]
    EDC --> COMPUTE
    GEN --> COMPUTE
```

`UnsupportedCohortError` carries a structured payload the agent
uses (SPEC-004) to emit a "general guidance only, please work with
your clinician" response — it is not an error surfaced to the
user as a failure.

### 4.10 Rationale (`rationale.py`)

```python
@dataclass(frozen=True)
class RationaleStep:
    id: str                      # e.g. "bmr_mifflin_female"
    label: str                   # human-readable
    inputs: dict[str, Any]       # inputs this step consumed
    outputs: dict[str, Any]      # outputs this step produced
    source: str                  # equation citation
    note: Optional[str] = None

@dataclass(frozen=True)
class Rationale:
    steps: tuple[RationaleStep, ...]
    applied_overrides: tuple[str, ...]
    cohort: str
```

Every function in §4.3–§4.8 appends exactly one step. The rationale
is a complete, ordered audit of how the numbers were produced.

### 4.11 Versioning

`CALCULATOR_VERSION = "MAJOR.MINOR.PATCH"`:

- **MAJOR**: equation swap or cohort-routing change. Downstream cache
  invalidation is expected.
- **MINOR**: table update (DRI refresh, AMDR tweak). Invalidation
  expected.
- **PATCH**: bug fixes that do not alter outputs on valid inputs.

Tests include a **golden-output test** (§6.2) that pins outputs for
a fixed set of representative profiles. A MAJOR or MINOR bump
rewrites the goldens; a PATCH bump must not.

### 4.12 Priority-grouped work items

| # | Item | Priority |
|---|------|----------|
| W1 | Module scaffolding; `version.py`; `errors.py`; package exports | P0 |
| W2 | `rationale.py` dataclasses; tests for immutability and equality | P0 |
| W3 | `bmr.py` + unit tests against Mifflin/Katch worked examples | P0 |
| W4 | `tdee.py` + `pal.yaml` + tests | P0 |
| W5 | `energy_goal.py` + safety-floor tests | P0 |
| W6 | `macros.py` + `amdr.yaml` + AMDR-clamp tests (including fall-through when protein+fat exceed band) | P0 |
| W7 | `micros.py` + `dri.yaml` (v1 micro set) + tests per cohort row | P0 |
| W8 | `clinical_overrides.py` + ordered chain + coverage for v1 enum | P1 |
| W9 | `targets.py` orchestration + cohort routing + top-level tests | P0 |
| W10 | Golden-output tests for ~30 representative profiles | P1 |
| W11 | Benchmarks: `compute_daily_targets` p99 ≤ 5 ms on reference hardware | P2 |
| W12 | Citation pass on all YAML tables; reviewer sign-off | P1 |

---

## 5. Rollout Plan

This is a pure library; rollout is about getting it reviewed,
benchmarked, and pinned before SPEC-004 consumes it.

### Phase 0 — Scaffolding (P0)
- [ ] W1, W2 landed; imports resolve; CI runs empty test suite green.

### Phase 1 — Core equations (P0)
- [ ] W3 through W7 landed with cited tables.
- [ ] `pytest backend/agents/nutrition_meal_planning_team/nutrition_calc/` all green.
- [ ] Coverage ≥95% on `nutrition_calc/` (it is pure logic — high bar is correct).

### Phase 2 — Clinical and cohort routing (P0/P1)
- [ ] W8, W9 landed.
- [ ] UnsupportedCohortError payload includes structured reason that SPEC-004 can read.

### Phase 3 — Hardening (P1/P2)
- [ ] W10 golden outputs checked in; test fails on any output drift.
- [ ] W11 benchmarks checked in (pytest-benchmark); CI fails regressions.
- [ ] W12 external clinical reviewer sign-off on `dri.yaml` / `amdr.yaml` / `pal.yaml`.
- [ ] `CALCULATOR_VERSION` frozen at `1.0.0` and announced in CHANGELOG.

### Rollback
- This is additive. Module exists but is not called by any agent
  until SPEC-004 lands. Rollback = revert the PR.

---

## 6. Verification

### 6.1 Reference-value tests

For each equation, **table-driven tests** against published worked
examples:

- Mifflin–St Jeor: at least 6 worked examples per sex from
  peer-reviewed sources; tolerance ≤1 kcal.
- Katch–McArdle: at least 4 worked examples; tolerance ≤1 kcal.
- PAL multipliers: exact float equality to table values.
- AMDR clamps: at least one profile per band edge on each macro.
- DRI micros: one row per (sex × age-band × repro-state) combination
  in v1 coverage.

### 6.2 Golden-output tests

`tests/golden/` holds YAML files of (profile → CalculatorResult). The
test round-trips every profile through `compute_daily_targets` and
asserts byte-equality on the result (with `rationale` canonicalized).
Any output change fails the test. Intentional changes bump
`CALCULATOR_VERSION` and rewrite goldens in the same PR.

Profile fixture matrix (≥30):

- General adult, all activity levels × both reference sexes × three
  ages × goal types.
- Body-fat-present cases routed to Katch–McArdle.
- `sex=other` and `sex=unspecified` paths.
- Pregnancy T1/T2/T3 and lactation (0–6 mo, 6–12 mo).
- ED-history flag path.
- Each supported CKD stage (1–3) × one profile.
- Hypertension clamp profile.
- T2D per-meal carb-cap profile.
- Safety-floor trigger profile (aggressive deficit).
- `UnsupportedCohortError` path: minor, CKD stage 4–5.

### 6.3 Failure-mode tests

- `InsufficientInputError` when weight, height, sex, or age missing
  on a general-adult profile.
- `ImplausibleInputError` when bounds bypassed (simulate bad DB
  value).
- `UnsupportedCohortError` carries the expected `cohort` and
  `guidance_key` that SPEC-004 will route on.

### 6.4 Property tests

Via `hypothesis`:

- For all valid profiles, `CalculatorResult` satisfies invariants:
  `kcal_target ≥ floor`, protein in [0.8·kg, 2.2·kg] (except
  ed_adjacent cohort which stays at DRI-min), macros sum to
  `kcal_target` within rounding tolerance, every rationale step has
  non-empty `id` and `source`.
- Monotonicity: increasing `weight_kg` (holding all else equal) never
  decreases `protein_g` or `kcal_target` on the general-adult path.

### 6.5 Benchmark

- `compute_daily_targets` p99 ≤ 5 ms on CI reference runner. This is
  generous; the point is regression detection, not raw speed.

### 6.6 Review

- Clinical reviewer sign-off on `dri.yaml`, `amdr.yaml`, `pal.yaml`,
  and the override chain in `clinical_overrides.py:ORDER`. Sign-off
  recorded in PR description; name becomes the file's designated
  reviewer on future changes.
- Second engineer review on cohort routing — the single highest-risk
  branch in the module.

### 6.7 Cutover criteria

- All tests green, coverage ≥95%, goldens frozen, benchmarks
  baselined, reviewer sign-off recorded. Then and only then does
  SPEC-004 open.

---

## 7. Open Questions

- **Katch–McArdle preference when body fat is present but likely
  user-estimated.** We currently prefer it unconditionally when the
  field is set. Alternative: use it only when `body_fat_pct`'s
  `source` is a measurement integration (ADR-006). v1 goes with
  unconditional and documents the tradeoff.
- **Strength-training PAL vs. muscle-gain kcal.** `muscle` goal
  currently adds +250 kcal conditionally. Could be merged with an
  activity-specific PAL override. Deferred.
- **Whether to expose intermediate outputs** (BMR, TDEE) on
  `CalculatorResult` for debugging. Yes — add as `intermediates:
  dict[str, float]` alongside `targets` and `rationale`. Cheap and
  useful for the "why these numbers?" UI panel.
