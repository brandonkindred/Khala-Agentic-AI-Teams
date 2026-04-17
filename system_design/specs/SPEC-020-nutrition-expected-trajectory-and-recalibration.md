# SPEC-020: Expected trajectory model and recalibration loop

| Field       | Value                                                    |
|-------------|----------------------------------------------------------|
| **Status**  | Proposed                                                 |
| **Author**  | Nutrition & Meal Planning team                           |
| **Created** | 2026-04-17                                               |
| **Priority**| P1 within ADR-006 (blocks full dashboard; enables self-correcting targets) |
| **Scope**   | Extension of SPEC-003 calculator, new `trajectory/` module, new `recalibration/` module, Postgres snapshots, safety-rail enforcement, admin override audit |
| **Depends on** | SPEC-003 (calculator), SPEC-019 (biometric observations), SPEC-018 (cook events for adherence), SPEC-010 (nutrient rollup for plan totals) |
| **Implements** | ADR-006 §5 (expected-trajectory model), §6 (safety rails at calculator level), §7 (recalibration loop) |

---

## 1. Problem Statement

SPEC-003 computes daily targets. SPEC-019 stores observed weight.
SPEC-018 records what the user cooked. None of them speak to each
other. Without the bridge:

1. We cannot show a user an expected weight curve for their goal
   — so we cannot answer "is this working?"
2. We cannot detect when reality diverges from the math — so the
   calculator never learns.
3. Safety rails (rate-of-loss cap, BMI floor, ED-history rerouting)
   are theoretically in SPEC-003 but have no live observation data
   to react to.

This spec ships the expected-trajectory model, the recalibration
loop that Bayesian-blends observed outcome into the calculator's
TDEE estimate, and the runtime enforcement of the SPEC-003 safety
rails using SPEC-019's observation stream.

It is deliberately not the dashboard (SPEC-022). This is the
math and the policy; the UI is built on top.

---

## 2. Current State

### 2.1 After SPEC-003 + SPEC-019

- Calculator produces `DailyTargets` from profile biometrics.
- Observations stream in but nothing consumes them beyond the
  weight-trend event (SPEC-019 §4.9).
- `GoalsInfo.target_weight_kg` and `rate_kg_per_week` live on the
  profile but are static.

### 2.2 Gaps

1. No expected trajectory series.
2. No recalibration: the TDEE estimate cannot update from observed
   outcome.
3. Safety rails in SPEC-003 §4.5 (kcal floor, rate cap) are applied
   once at plan generation; they never react to persistent over-
   deficit in observed data.
4. No `nutrition_adherence_events` aggregator that pairs plan vs.
   cooked vs. observed over windows — ADR-006 §7 describes the
   recalibration inputs but nothing assembles them.

---

## 3. Goals and Non-Goals

### 3.1 Goals

- Ship `trajectory.compute_expected(profile, window)` producing a
  `Trajectory` series: per-date expected biometric values with a
  confidence band, grounded in the calculator's energy math.
- Ship `recalibration.evaluate(profile, adherence_window)` that
  detects persistent divergence between expected deficit-
  equivalent and observed weight change, controlled for plan +
  target adherence.
- Recalibration is **proposal-only**: it surfaces a suggested TDEE
  correction, never silently rewrites targets. A user or admin
  accepts via endpoint; acceptance bumps the profile's TDEE
  adjustment and writes a new plan version.
- Enforce safety rails in a live loop:
  - Rate-of-loss cap from observation stream.
  - BMI-floor disable of weight-loss goals.
  - ED-history flag replaces scale-centric views.
- Store `trajectory_snapshots` and `adherence_snapshots` as
  materialized inputs for SPEC-021 drivers and SPEC-022 dashboard.
- Clinical-cohort escape: pregnancy, lactation, post-op cohorts
  return `trajectory=None` with a "work with your clinician"
  note. Never guess.

### 3.2 Non-goals

- **No dashboard UI.** SPEC-022.
- **No drivers decomposition.** SPEC-021.
- **No recalibration auto-accept.** Always proposal → user action.
- **No long-horizon prediction beyond the user's goal window.**
  Trajectories extend only as far as the user's
  `target_weight_kg` plus 4 weeks of buffer.
- **No metabolic-adaptation research.** We use a simple,
  documented drift term; novel modeling is out of scope.

---

## 4. Detailed Design

### 4.1 Module layout

```
backend/agents/nutrition_meal_planning_team/trajectory/
├── __init__.py              # compute_expected, TRAJECTORY_VERSION
├── version.py               # TRAJECTORY_VERSION = "1.0.0"
├── types.py                 # Trajectory, TrajectoryPoint
├── model.py                 # energy-balance model + adaptation term
├── snapshots.py             # Postgres persistence
├── errors.py
└── tests/

backend/agents/nutrition_meal_planning_team/recalibration/
├── __init__.py              # evaluate, accept, RECALIBRATION_VERSION
├── version.py               # RECALIBRATION_VERSION = "1.0.0"
├── types.py                 # RecalibrationProposal, AdherenceWindow
├── evaluator.py             # Bayesian TDEE update rule
├── safety_rails.py          # rate-of-loss cap, BMI floor, ED reroute
├── store.py                 # proposal persistence and accept audit
├── errors.py
└── tests/
```

### 4.2 Trajectory model

```python
@dataclass(frozen=True)
class TrajectoryPoint:
    date: date
    expected_value: float       # e.g. expected weight_kg
    lower_band: float           # confidence band
    upper_band: float
    inputs_snapshot: dict       # which inputs drove this point

@dataclass(frozen=True)
class Trajectory:
    kind: ObservationKind       # v1: weight_kg only
    series: tuple[TrajectoryPoint, ...]
    cohort: str                 # 'general_adult' | 'pregnancy_lactation' | ...
    calculator_version: str
    trajectory_version: str
    computed_at: str
```

Model (v1, `weight_kg` only):

- For each day in the window:
  ```
  cumulative_deficit_kcal = Σ_days (prescribed_kcal_target - TDEE_current)
  expected_delta_kg       = cumulative_deficit_kcal / 7700
  early_phase_correction  = water/glycogen ≈ -0.7 kg for first 14 days on deficit
  adaptation_drift        = +0.0005 * cumulative_deficit_kcal / 7700   (a small recovery term)
  expected_weight_kg[day] = baseline_kg + expected_delta + correction + adaptation_drift
  ```
- Confidence band: ±1% of body weight widening to ±3% over the
  horizon; derived from the standard deviation of daily weight
  variability observed in calibration datasets and cited in
  `model.py` comments.

The math is deterministic. The model constants (`7700 kcal/kg`,
the water/glycogen term, the adaptation coefficient) are pinned
in `tables/trajectory.yaml` with citations; bumping any of them
bumps `TRAJECTORY_VERSION`.

Cohorts without a supported trajectory return `Trajectory.kind =
None` with `cohort = 'clinician_guided'` or `'pregnancy_lactation'`
and no series. SPEC-022 renders a distinct view.

### 4.3 Recalibration evaluator

Inputs over a sliding window (default 21 days, minimum 14):

- Observed weight change: smoothed 7-day EMA of daily weights.
- Plan adherence: fraction of planned recipes cooked (`made`
  weight 1.0, `partial` proportional, `swapped` at substitute
  nutrients, `skipped` 0.0).
- Target adherence: eaten nutrient rollup (from SPEC-018 cook
  events + SPEC-019 off-plan intakes) vs. daily targets, expressed
  as a per-day deficit delta.
- Expected TDEE from the calculator.

Procedure:

1. Compute **observed deficit-equivalent**:
   ```
   observed_Δmass_kg  = EMA(end) - EMA(start)
   observed_deficit   = observed_Δmass_kg * 7700          # kcal
   ```
2. Compute **intake-vs-prescription adjustment**:
   ```
   intake_observed     = Σ eaten_kcal over window
   intake_prescribed   = Σ target_kcal over window
   intake_delta        = intake_observed - intake_prescribed
   ```
   `intake_observed` from SPEC-018/SPEC-019; `intake_prescribed`
   from the target history.
3. Infer implied TDEE:
   ```
   implied_TDEE = (intake_observed - observed_deficit) / window_days
   ```
4. Blend with a-priori TDEE:
   ```
   posterior_TDEE = (w_prior * prior_TDEE + w_data * implied_TDEE) /
                    (w_prior + w_data)
   ```
   `w_prior` starts at 30 (roughly 3 weeks of data-equivalent
   prior), `w_data` = window_days. This is a simple Bayesian
   blend; calibration is a team-lead decision.
5. Guard rails: only propose if:
   - |posterior_TDEE - prior_TDEE| > 75 kcal (noise floor).
   - Window has ≥ 14 days of data and ≥ 70% adherence. Below,
     decline to propose — "need more data" is a first-class
     output, not a failure.
   - Last proposal accepted or rejected ≥ 21 days ago
     (anti-thrashing).

Output:

```python
@dataclass(frozen=True)
class RecalibrationProposal:
    proposal_id: str
    client_id: str
    window_start: date
    window_end: date
    prior_tdee: float
    implied_tdee: float
    posterior_tdee: float
    delta_kcal: float              # round(posterior - prior)
    confidence: float              # 0..1; function of window_days + adherence
    explanation: str               # short NL for UI ("maintenance closer to 2,250 than 2,400")
    expires_at: str                # TTL 30 days
```

### 4.4 Proposal lifecycle

- Evaluated on a schedule (nightly) plus on-demand via admin
  endpoint.
- Persisted in `nutrition_recalibration_proposals`.
- User sees the proposal in the dashboard (SPEC-022); accepts or
  declines.
- Acceptance writes a `tdee_adjustment_kcal` on the profile (new
  additive field) that SPEC-003's calculator reads and applies as
  a post-TDEE multiplier-equivalent. New plans generated with the
  new target; prior plans untouched.
- Decline records the choice; no further proposal for 21 days
  unless a new rail breach requires one.
- Auto-accept is not offered. Targets moving silently erodes trust.

### 4.5 Safety rails in the evaluator

Three rails enforced at runtime on every trajectory compute +
every new weight observation (via the SPEC-019 weight-trend event):

1. **Rate-of-loss cap.** If the 2-week EMA shows > 1% body weight
   loss per week for two consecutive weeks, the calculator's
   `energy_goal` is force-clamped: `rate_kg_per_week` reduced
   until trajectory expected rate ≤ 0.7% per week. The profile
   is flagged `rate_cap_active=true`; plans generated while
   flagged carry the reason. User can override only with an
   explicit acknowledgment dialog and one confirmation.
2. **BMI floor.** If BMI (from latest weight + height) drops below
   the greater of 18.5 and `clinician_overrides.bmi_floor`,
   weight-loss goals are disabled at the calculator level. Next
   plan is guidance-only; dashboard replaces trajectory gauges
   with "revisit your goal" prompt.
3. **ED-history flag.** When `clinical.ed_history_flag=true`, the
   evaluator never emits a recalibration proposal that tightens
   a deficit. Proposals are filtered to net-neutral or net-loose
   only. Dashboard rails already hide scale-centric views.

Minors (`age_years < 18`): cohort routed to `minor` by SPEC-003;
trajectory = None; no recalibration ever evaluated.

Rails are **team invariants**, not per-user toggles. Overrides
travel through authorized `clinician_overrides`.

### 4.6 Persistence

Migration `017_trajectory_and_recalibration.sql`:

```sql
CREATE TABLE IF NOT EXISTS nutrition_trajectory_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    client_id         TEXT NOT NULL REFERENCES nutrition_profiles(client_id)
                          ON DELETE CASCADE,
    kind              TEXT NOT NULL,
    profile_version   INT NOT NULL,
    calculator_version TEXT NOT NULL,
    trajectory_version TEXT NOT NULL,
    series_json       JSONB NOT NULL,
    cohort            TEXT NOT NULL,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON nutrition_trajectory_snapshots (client_id, computed_at DESC);

CREATE TABLE IF NOT EXISTS nutrition_recalibration_proposals (
    proposal_id       TEXT PRIMARY KEY,
    client_id         TEXT NOT NULL REFERENCES nutrition_profiles(client_id)
                          ON DELETE CASCADE,
    window_start      DATE NOT NULL,
    window_end        DATE NOT NULL,
    prior_tdee        DOUBLE PRECISION NOT NULL,
    implied_tdee      DOUBLE PRECISION NOT NULL,
    posterior_tdee    DOUBLE PRECISION NOT NULL,
    delta_kcal        DOUBLE PRECISION NOT NULL,
    confidence        REAL NOT NULL,
    explanation       TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'proposed',    -- proposed | accepted | declined | expired
    responded_at      TIMESTAMPTZ,
    expires_at        TIMESTAMPTZ NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nutrition_safety_rail_events (
    id                BIGSERIAL PRIMARY KEY,
    client_id         TEXT NOT NULL REFERENCES nutrition_profiles(client_id)
                          ON DELETE CASCADE,
    rail              TEXT NOT NULL,         -- 'rate_of_loss' | 'bmi_floor' | 'ed_filter'
    status            TEXT NOT NULL,         -- 'triggered' | 'cleared' | 'override_requested'
    payload_json      JSONB NOT NULL,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Additive on nutrition_profiles
ALTER TABLE nutrition_profiles
    ADD COLUMN tdee_adjustment_kcal REAL NOT NULL DEFAULT 0.0,
    ADD COLUMN rate_cap_active BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN last_trajectory_snapshot_id TEXT,
    ADD COLUMN last_recalibration_accepted_at TIMESTAMPTZ;
```

### 4.7 API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/trajectory/{client_id}?kind=weight_kg&window=next_12w` | Latest trajectory snapshot |
| `POST` | `/trajectory/{client_id}/recompute` | Force recompute (rate-limited) |
| `GET` | `/recalibration/{client_id}/proposal` | Latest active proposal (if any) |
| `POST` | `/recalibration/{client_id}/accept` | Body: `{proposal_id}`. Applies adjustment and writes new plan version in next generation |
| `POST` | `/recalibration/{client_id}/decline` | Declines the proposal |
| `GET` | `/safety/{client_id}/rails` | Current rail states |

The trajectory endpoint returns either the latest snapshot or
triggers a fresh compute asynchronously; it never recomputes on
the critical path (cached snapshots only).

### 4.8 Nightly job

A scheduled task (Temporal-backed, via platform scheduled tasks):

1. For each active client, check whether a new trajectory snapshot
   should be produced (≥7 days since last, or any new weight
   observation, or any new recalibration acceptance).
2. Compute trajectory snapshot; persist.
3. Evaluate recalibration proposal on the 21-day window; if
   warranted, persist proposal and emit notification.
4. Evaluate safety rails on current observation stream; emit
   `nutrition_safety_rail_events` rows on state changes.

Client-perceived behavior: open the dashboard → see current
trajectory + any pending proposal.

### 4.9 Observability

- `trajectory.snapshot_computed{cohort}`.
- `trajectory.compute_latency_ms` histogram.
- `recalibration.evaluated{outcome}` — `proposed | need_more_data |
  noise_floor | anti_thrash | cohort_blocked`.
- `recalibration.proposal.lifecycle{status}`.
- `safety_rail.triggered{rail}`.
- `safety_rail.cleared{rail}`.
- `safety_rail.override_requested{rail}`.
- Alert: any `safety_rail.override_requested{rail=bmi_floor}` →
  privacy-respecting on-call notification for review.

### 4.10 Privacy

- Trajectory and recalibration are derived from sensitive data;
  inherit the same retention and cascade-delete as SPEC-019.
- Recalibration `explanation` strings include kcal numbers only,
  no PII.
- Safety-rail events are logged; raw observation values are not
  in the log payload — only aggregates.

### 4.11 Priority-grouped work items

| # | Item | Priority |
|---|------|----------|
| W1 | Trajectory module scaffolding, version, types | P0 |
| W2 | `model.py` energy-balance + adaptation + confidence band; tests | P0 |
| W3 | Trajectory snapshot persistence | P0 |
| W4 | Recalibration module scaffolding + types | P0 |
| W5 | `evaluator.py` Bayesian blend + guard rails; tests | P0 |
| W6 | `safety_rails.py` runtime enforcement + calculator integration | P0 |
| W7 | Migration `017_trajectory_and_recalibration.sql` | P0 |
| W8 | API endpoints (§4.7) | P0 |
| W9 | Nightly scheduled task wiring | P1 |
| W10 | `tdee_adjustment_kcal` integration into SPEC-003 calculator | P0 |
| W11 | Observability counters | P1 |
| W12 | Cohort routing for pregnancy/lactation/minors (no trajectory) | P0 |
| W13 | ED-history filtered proposals | P0 |
| W14 | Admin CLI: inspect a user's recent proposals + rails | P2 |
| W15 | Benchmarks: trajectory compute p99 ≤ 150 ms; evaluator p99 ≤ 200 ms | P2 |

---

## 5. Rollout Plan

Two flags:

- `NUTRITION_TRAJECTORY` (off → no trajectory snapshots; on →
  computed and served).
- `NUTRITION_RECALIBRATION` (off → no proposals; on → proposals
  computed and surfaced).

The safety rails ship with `NUTRITION_TRAJECTORY` on, always — they
are invariants, not user features.

### Phase 0 — Foundation (P0)
- [ ] SPEC-018 and SPEC-019 at 100% ramp.
- [ ] W1–W7, W10, W12 landed.

### Phase 1 — Trajectory shadow (P0)
- [ ] Flag on internal; trajectory snapshots computed nightly.
- [ ] Review 10 internal trajectories over 30 days against actual
      outcomes. Acceptance: predicted weight within ±1.5 kg on
      80% of points for steady-state users.
- [ ] Safety rails enforced for internal; review triggers.

### Phase 2 — Recalibration shadow (P0)
- [ ] W5, W6, W8, W13 landed.
- [ ] Shadow mode: recalibration proposals computed for internal
      users but not surfaced; team reviews whether they would
      have been helpful.
- [ ] Acceptance: ≥80% of would-have-been proposals judged
      correct by clinical reviewer.

### Phase 3 — User-facing ramp (P1)
- [ ] Both flags on internal, then 10% → 50% → 100% over three
      weeks.
- [ ] Watch: proposal acceptance rate (too high → we are too
      aggressive; too low → insufficient evidence gate).
- [ ] Safety-rail triggers reviewed weekly.

### Phase 4 — Cleanup (P1/P2)
- [ ] W9 nightly job wiring hardened.
- [ ] W11, W14, W15 landed.

### Rollback
- Flag off → no trajectory or proposals surfaced; stored
  snapshots retained.
- Safety rails remain on regardless of flag — they are team
  invariants.
- `tdee_adjustment_kcal` changes persisted across rollback; users
  keep their accepted adjustments.

---

## 6. Verification

### 6.1 Unit tests

- `test_trajectory_deterministic.py` — same inputs → byte-equal
  series.
- `test_trajectory_early_phase.py` — first 14 days include the
  water/glycogen correction.
- `test_trajectory_confidence_band.py` — band widens over horizon.
- `test_recalibration_noise_floor.py` — small deltas do not produce
  proposals.
- `test_recalibration_insufficient_data.py` — windows with <14
  days produce `need_more_data`, not a proposal.
- `test_recalibration_anti_thrash.py` — proposal within 21 days
  of last outcome → declined at evaluator level.
- `test_ed_filter_blocks_deficit_proposal.py`.

### 6.2 Integration tests

- `test_trajectory_snapshot_persisted.py` — compute → snapshot
  row with correct fields; downstream query via API returns
  identical data.
- `test_recalibration_accept_writes_adjustment.py` — accepted
  proposal updates `tdee_adjustment_kcal`; next SPEC-003
  calculator call returns adjusted targets.
- `test_rate_cap_rail.py` — insert a weight series triggering >1%
  / week loss; rail event emitted; calculator clamps goal on
  next generation; profile flag set.
- `test_bmi_floor_rail.py` — weight loss into BMI <18.5 → rail
  event + goal disabled.
- `test_minor_no_trajectory.py` — age<18 profile returns
  `Trajectory.kind=None`; no proposals ever generated.

### 6.3 Shadow-phase validation

- Phase 1: 10 internal trajectories vs. observed. Reviewer
  acceptance gate.
- Phase 2: 10 would-have-been proposals reviewed; ≥80% judged
  correct by clinical reviewer.

### 6.4 Safety property tests

- Rails cannot be bypassed by API: attempt to generate a plan
  while `rate_cap_active=true` → SPEC-003 calculator clamps the
  deficit regardless of request body.
- ED-history flag blocks deficit proposals in all code paths.

### 6.5 Observability

All §4.9 counters emit; safety-rail alerting path exercised in
staging.

### 6.6 Cutover criteria

- All P0/P1 tests green.
- Phase 1 trajectory accuracy gate met.
- Phase 2 recalibration reviewer acceptance met.
- Phase 3 ramp metrics stable.
- Clinical reviewer sign-off on `model.py` constants and the
  proposal evaluator thresholds.

---

## 7. Open Questions

- **Calibration of the water/glycogen correction.** We use a
  conservative -0.7 kg over 14 days. Individual variance is high;
  we will refine after Phase 1 against real observations.
- **`w_prior` weighting.** 30 "days of prior" is a choice. Too
  high and proposals lag; too low and we over-react to noise.
  Tunable per `recalibration.evaluated` dashboard.
- **Multi-kind trajectories.** v1 only models weight. Blood
  glucose and blood pressure trajectories under clinical-
  condition cohorts are v1.1 candidates but require clinician
  input and have different acceptable-range definitions.
- **Real-world adherence proxy from cook events.** We use
  `made/partial/skipped` weights for adherence. If users
  consistently under-log (e.g. cook but don't tap), adherence
  looks artificially low. Counter-measure: SPEC-022 surfaces the
  log rate and nudges low-log users; we accept imperfect
  adherence data rather than guessing.
- **Accepted-proposal visibility to clinicians.** The
  `nutrition_recalibration_proposals` history is a natural export
  for clinicians. v1.1 clinician export spec.
