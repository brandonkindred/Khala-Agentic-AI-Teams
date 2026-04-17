# SPEC-019: Biometric observations store and off-plan intake log

| Field       | Value                                                    |
|-------------|----------------------------------------------------------|
| **Status**  | Proposed                                                 |
| **Author**  | Nutrition & Meal Planning team                           |
| **Created** | 2026-04-17                                               |
| **Priority**| P0 within ADR-006 (blocks SPEC-020, SPEC-022)            |
| **Scope**   | New module `backend/agents/nutrition_meal_planning_team/biometrics/`, new `off_plan_intake/`, Postgres tables, API endpoints, UI entry screens |
| **Depends on** | SPEC-002 (profile biometrics + timezone), SPEC-005 (parser for quick-log) |
| **Implements** | ADR-006 §2 (biometric observations), §4 (off-plan intake logging) |

---

## 1. Problem Statement

ADR-006's dashboard needs two data inputs that do not yet exist:

1. **Biometric observations over time** — weight, waist, body-fat
   percentage, resting heart rate, blood pressure, fasting glucose,
   A1c, lipid panels — the outcome side of the adherence ledger.
2. **Off-plan intake** — what the user actually ate outside the
   plan. Without this, target adherence is wrong in one direction
   by construction: users who eat an off-plan lunch look like they
   skipped lunch.

This spec ships both stores. It is deliberately boring data
infrastructure — no computation, no dashboard, no trajectory model.
That logic lives in SPEC-020 and SPEC-021. This spec is the
foundation they sit on.

The scope decision matters: biometric data is high-sensitivity and
lends itself to anxiety-inducing UX. We ship the store first with
privacy, retention, and safety guardrails already in place, before
any dashboard exists to present it.

---

## 2. Current State

### 2.1 What's there

- SPEC-002 extended `BiometricInfo` on `ClientProfile` with
  latest-values (`weight_kg`, `height_cm`, etc.) and a
  `nutrition_biometric_log` table for the audit trail on profile
  writes.
- `timezone` on profile.
- No generalized time-series biometric store.
- No off-plan intake concept anywhere.

### 2.2 Gaps

1. `nutrition_biometric_log` from SPEC-002 only records writes
   tied to profile edits — it is not a time-series store with
   proper ingestion, device-agnostic sourcing, or multiple per-
   day observations.
2. No way to log a meal the user ate that was not on their plan.
3. No device-integration-ready schema — the ADR-006 roadmap
   explicitly relies on Apple Health, Google Fit, Withings,
   Fitbit adapters sitting on this schema unchanged.

---

## 3. Goals and Non-Goals

### 3.1 Goals

- Ship `nutrition_biometric_observations` as the canonical time-
  series store for biometric data. Device integrations sit on top
  unchanged.
- Ship CRUD API for manual observation entry.
- Ship `nutrition_off_plan_intakes` with a quick-log endpoint:
  free-text input → SPEC-005 + LLM parse → estimated nutrients
  stored with confidence.
- Enforce safety rails on data quality: implausible observations
  rejected, not clamped. Source tracking on every row.
- Treat both data sets as privacy-sensitive: redaction, cascade
  delete, least-logging principle.

### 3.2 Non-goals

- **No trajectory model.** SPEC-020.
- **No adherence computation.** SPEC-021.
- **No dashboard.** SPEC-022.
- **No device integrations in v1.** Schema supports them; the
  adapters are follow-up specs.
- **No photo-based logging.** Text quick-log only in v1.
- **No clinician-export format.** v1.1 feature; ADR-006 §7
  follow-up.

---

## 4. Detailed Design

### 4.1 Module layout

```
backend/agents/nutrition_meal_planning_team/biometrics/
├── __init__.py            # store, types, BIOMETRIC_VERSION
├── version.py             # BIOMETRIC_VERSION = "1.0.0"
├── types.py               # Observation, ObservationKind
├── store.py               # CRUD + time-series queries
├── validation.py          # implausibility guards
├── errors.py
└── tests/

backend/agents/nutrition_meal_planning_team/off_plan_intake/
├── __init__.py            # quick_log, store
├── types.py               # OffPlanIntake, QuickLogDraft
├── parser.py              # SPEC-005 + LLM fallback
├── store.py               # CRUD
├── errors.py
└── tests/
```

### 4.2 Biometric observation kinds

Closed enum (v1):

```python
class ObservationKind(str, Enum):
    weight_kg = "weight_kg"
    waist_cm = "waist_cm"
    hip_cm = "hip_cm"
    body_fat_pct = "body_fat_pct"
    resting_hr_bpm = "resting_hr_bpm"
    bp_systolic = "bp_systolic"
    bp_diastolic = "bp_diastolic"
    fasting_glucose_mgdl = "fasting_glucose_mgdl"
    a1c_pct = "a1c_pct"
    ldl_mgdl = "ldl_mgdl"
    hdl_mgdl = "hdl_mgdl"
    triglycerides_mgdl = "triglycerides_mgdl"
    total_cholesterol_mgdl = "total_cholesterol_mgdl"

class ObservationSource(str, Enum):
    manual = "manual"
    apple_health = "apple_health"
    google_fit = "google_fit"
    withings = "withings"
    fitbit = "fitbit"
    lab_upload = "lab_upload"
    clinician = "clinician"
```

Additions require minor version bump.

### 4.3 Observation type

```python
@dataclass(frozen=True)
class Observation:
    id: str
    client_id: str
    kind: ObservationKind
    value: float
    unit: str                      # canonical for the kind ('kg', 'cm', 'pct', 'bpm', 'mm_hg', 'mg_dl', 'percent')
    observed_at: str               # ISO timestamp with timezone
    source: ObservationSource
    source_ref: Optional[str] = None   # e.g. device serial, lab upload id
    notes: Optional[str] = None
    confidence: float = 1.0        # 1.0 manual + non-implausible, <1.0 from device with noise
    recorded_at: str
```

### 4.4 Implausibility guards

`validation.py::validate(obs) -> Observation`:

- `weight_kg`: [20, 400].
- `waist_cm`: [30, 250].
- `body_fat_pct`: [2, 75].
- `resting_hr_bpm`: [30, 220].
- `bp_systolic`: [60, 260]. `bp_diastolic`: [30, 180].
  `bp_systolic > bp_diastolic` enforced.
- `fasting_glucose_mgdl`: [30, 600].
- `a1c_pct`: [3, 20].
- Lipids: generous but reject absurd values (e.g. `ldl_mgdl <
  10` or `> 500`).

Outside-range → 422. We do **not** clamp. Device-sourced
observations with outside-range values get a structured log entry
and are dropped, with a counter; users see a UI notification only
for manual entries.

### 4.5 Postgres

Migration `015_biometric_observations.sql`:

```sql
CREATE TABLE IF NOT EXISTS nutrition_biometric_observations (
    id              TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL REFERENCES nutrition_profiles(client_id)
                        ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    value           DOUBLE PRECISION NOT NULL,
    unit            TEXT NOT NULL,
    observed_at     TIMESTAMPTZ NOT NULL,
    source          TEXT NOT NULL,
    source_ref      TEXT,
    notes           TEXT,
    confidence      REAL NOT NULL DEFAULT 1.0,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (client_id, kind, observed_at, source)  -- idempotency for device adapters
);
CREATE INDEX ON nutrition_biometric_observations (client_id, kind, observed_at DESC);
CREATE INDEX ON nutrition_biometric_observations (client_id, recorded_at DESC);
```

Unique index on `(client_id, kind, observed_at, source)` makes
device-adapter ingestion idempotent — replaying a sync window
does not duplicate rows.

Migration `016_off_plan_intakes.sql`:

```sql
CREATE TABLE IF NOT EXISTS nutrition_off_plan_intakes (
    id                    TEXT PRIMARY KEY,
    client_id             TEXT NOT NULL REFERENCES nutrition_profiles(client_id)
                              ON DELETE CASCADE,
    raw_description       TEXT NOT NULL,
    parsed_ingredients_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    estimated_nutrients_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence            REAL NOT NULL DEFAULT 0.5,
    meal_type             TEXT,
    approx_portion        TEXT,
    occurred_at           TIMESTAMPTZ NOT NULL,
    recorded_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON nutrition_off_plan_intakes (client_id, occurred_at DESC);
```

### 4.6 Biometric API

| Method | Path | Purpose |
|--------|------|---------|
| `POST`   | `/biometrics/{client_id}/observations` | Single or batch insert |
| `GET`    | `/biometrics/{client_id}/observations?kind=&since=&until=` | Time-series query with pagination |
| `DELETE` | `/biometrics/{client_id}/observations/{id}` | Remove a specific observation |
| `GET`    | `/biometrics/{client_id}/latest` | Latest value per kind |
| `GET`    | `/biometrics/{client_id}/series?kind=weight_kg&since=&smooth=7d` | Smoothed series for SPEC-020 |

Batch insert body is `{observations: [Observation], idempotent: bool}`.
When `idempotent=true`, duplicates (matching unique index) silently
no-op. When false (default), duplicates 409.

The smoothed-series endpoint is a convenience: returns EMA with
configurable window for UI charts. The server-side smoothing is
deterministic and cached.

### 4.7 Off-plan intake API

| Method | Path | Purpose |
|--------|------|---------|
| `POST`   | `/intake/{client_id}/quick-log` | `{description, meal_type?, approx_portion?, occurred_at?}` |
| `POST`   | `/intake/{client_id}/quick-log/{draft_id}/confirm` | Accept / modify parsed intake |
| `GET`    | `/intake/{client_id}?since=&until=` | List intakes |
| `DELETE` | `/intake/{client_id}/{id}` | Remove |
| `PATCH`  | `/intake/{client_id}/{id}` | Edit a saved intake |

Quick-log flow:

1. Run SPEC-005 parser on each comma/"and"-separated fragment.
2. For fragments that do not resolve, call an LLM with a locked
   structured-output schema (same pattern as SPEC-015's bulk
   import) producing `ProposedIntakeItem[]` with canonical id,
   portion, and confidence.
3. Estimate nutrients from SPEC-008 data per parsed item.
4. Return a draft preview (same pattern as SPEC-015).
5. Confirmation commits; unresolved items are preserved as notes.

Confidence on every intake: `avg(parsed_item_confidence)` weighted
by resolved-mass. Intakes below `confidence = 0.5` are marked
"best effort" and surface distinctly in the UI. The goal is
honest wrongness-quantification, not clean-looking data.

### 4.8 Log retention and privacy

- Observations and intakes are highly sensitive.
- Log redaction: observation values and raw intake descriptions
  never logged above DEBUG.
- Cascade delete on profile removal.
- No cross-team reads; only the nutrition team's dashboard and
  trajectory model consume.
- Audit events emitted on every write, read, and delete for
  biometric observations (compliance-adjacent).

### 4.9 Safety hooks

ADR-006 §6 defines rate-of-loss, BMI floor, and ED-history rails
that live in SPEC-003 calculator. This spec feeds them:

- On every `weight_kg` insert, a check computes the last-14-days
  trend and emits a structured event
  `nutrition.weight_trend_update{client_id, kg_per_week}`.
  SPEC-020 and SPEC-003 subscribe and apply rails.
- This spec does **not** clamp goals or modify plans. It signals;
  the calculator decides.

### 4.10 Observability

- `biometric.observation.recorded{kind, source}`.
- `biometric.observation.implausible_reject{kind, source}`.
- `biometric.observation.duplicate_ignored{source}` (idempotent
  ingests).
- `off_plan_intake.logged{confidence_bucket}`.
- `off_plan_intake.llm_fallback_used`.
- `biometric.weight_trend_event{direction}` counter.

### 4.11 Priority-grouped work items

| # | Item | Priority |
|---|------|----------|
| W1 | Biometric module scaffolding + types + version | P0 |
| W2 | Migration `015_biometric_observations.sql` | P0 |
| W3 | `validation.py` with range tests per kind | P0 |
| W4 | `store.py` CRUD + idempotent insert + time-series query | P0 |
| W5 | Smoothed-series endpoint (EMA) + tests | P1 |
| W6 | Weight-trend event emission | P0 |
| W7 | Off-plan intake module scaffolding + types | P0 |
| W8 | Migration `016_off_plan_intakes.sql` | P0 |
| W9 | Off-plan quick-log parser (SPEC-005 + LLM) + confirm flow | P0 |
| W10 | Intake API endpoints | P0 |
| W11 | UI: biometric entry screen (weight first; others secondary) | FE | P1 |
| W12 | UI: quick-log intake widget (text field + preview modal) | FE | P1 |
| W13 | UI: history tables (biometric + intake) | FE | P1 |
| W14 | Observability counters | P1 |
| W15 | Audit-event logging | P1 |
| W16 | Benchmarks: insert p99 ≤ 50 ms; series query p99 ≤ 100 ms | P2 |

---

## 5. Rollout Plan

Flag `NUTRITION_OBSERVATIONS` (off → endpoints hidden; on →
surfaced).

### Phase 0 — Foundation (P0)
- [ ] W1–W4, W7–W10 landed. Migrations in staging.

### Phase 1 — Core behind flag (P0)
- [ ] W6 weight-trend event emitted.
- [ ] Flag on internal. Team members log their own weight daily
      and off-plan meals; validate ingest paths.

### Phase 2 — UI (P1)
- [ ] W11–W13 shipped.
- [ ] Acceptance gate: 10 team users log ≥14 days each; zero
      implausibility false-rejects; zero data-loss bugs.

### Phase 3 — Ramp (P1)
- [ ] 10% → 50% → 100% over two weeks.
- [ ] Metrics: active-logger fraction (want ≥30% of active users
      log at least once per week), intake-logging rate on days
      with a planned meal count mismatch.

### Phase 4 — Cleanup (P1/P2)
- [ ] W5, W14, W15, W16 landed.
- [ ] Flag default on.

### Rollback
- Flag off → endpoints 404; rows retained.
- Additive migration.

---

## 6. Verification

### 6.1 Unit tests

- `test_validation_ranges.py` — every kind's bounds accept within,
  reject outside. 422 status.
- `test_bp_pair.py` — systolic < diastolic is rejected.
- `test_idempotent_insert.py` — same kind/time/source insert
  silently no-ops when `idempotent=true`; otherwise 409.
- `test_smoothed_series.py` — EMA window parameter honored;
  deterministic output.
- `test_off_plan_parse_llm_fallback.py` — unresolvable fragments
  trigger LLM fallback; parser-only path does not call LLM.

### 6.2 Integration tests

- `test_biometric_time_series.py` — insert 30 days of weight →
  `/series?smooth=7d` returns expected EMA.
- `test_weight_trend_event.py` — inserting a run of weights
  producing > 1% body weight / week loss emits the trend event
  with correct direction and magnitude.
- `test_off_plan_quick_log_roundtrip.py` — draft → confirm →
  stored intake has expected nutrients from SPEC-008.
- `test_cascade_delete.py` — deleting profile removes observations
  and intakes.

### 6.3 Privacy

- Log-redaction grep in staging over Phase 1: zero biometric
  values or intake descriptions above DEBUG.
- Audit-event rows present for every observation insert / delete
  during dogfood.

### 6.4 Reviewer audit (Phase 2)

- Review 20 off-plan quick-log parses. Target: ≥80% correct at
  item + quantity level; zero fabricated items (items not in the
  original description).

### 6.5 Observability

All §4.10 counters emit.

### 6.6 Cutover criteria

- All P0/P1 tests green.
- Phase 2 reviewer gate met.
- Phase 3 metrics at 10%: active-logger fraction tracking, no
  correctness incidents.

---

## 7. Open Questions

- **Units.** We store canonical units (kg, cm, mg/dl). US users
  enter in lbs/in/mg-dL. Conversion happens at input, same
  pattern as SPEC-002. UI concern.
- **Observation sources beyond v1 enum.** Continuous glucose
  monitors, research devices — add as they land; minor version
  bumps.
- **Batch quick-log.** Some users will want to log a day's worth
  of meals at once. v1 handles it via repeated calls; a batch API
  is v1.1.
- **Clinician observations.** A clinician-entered lab upload with
  `source=clinician` is allowed. We do not yet have a clinician
  portal; v1.1.
- **Retention horizons.** v1 keeps forever. Long-term we will want
  an archive tier for data older than the dashboard shows. Out of
  scope.
