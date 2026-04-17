# SPEC-021: Adherence ledger and structured drivers decomposition

| Field       | Value                                                    |
|-------------|----------------------------------------------------------|
| **Status**  | Proposed                                                 |
| **Author**  | Nutrition & Meal Planning team                           |
| **Created** | 2026-04-17                                               |
| **Priority**| P0 within ADR-006 (blocks SPEC-022 dashboard)            |
| **Scope**   | New module `backend/agents/nutrition_meal_planning_team/adherence/`, nightly snapshot job, structured drivers API, feedback loop into SPEC-012 learned preferences |
| **Depends on** | SPEC-003 targets, SPEC-010 plan rollup, SPEC-018 cook events, SPEC-019 observations + off-plan intake, SPEC-020 trajectory |
| **Implements** | ADR-006 §1 (three adherence questions), §3 (eaten rollup), §9 (drivers), §10 (planner feedback) |

---

## 1. Problem Statement

ADR-006 promises three separate, non-conflated answers: did the
user cook the plan (plan adherence), did what they ate hit the
targets (target adherence), did the outcome move toward the goal
(goal progress). SPEC-019 and SPEC-018 produce the raw inputs;
SPEC-020 produces the expected trajectory. This spec is the
**aggregation** layer that combines them into per-window snapshots
with a structured decomposition of which factors are driving the
gaps.

This spec is not a UI. It produces:

1. `nutrition_adherence_snapshots` — materialized per-user per-
   window rows that the dashboard reads O(1).
2. A `drivers` structured payload that names the top contributors
   to any gap ("three skipped lunches account for 18 g/day of your
   protein gap"). This is the decomposition layer the dashboard
   renders and the planner (SPEC-012) consumes.

Keeping aggregation in its own spec keeps the dashboard fast and
keeps the feedback loop into the planner honest — the drivers are
the signal, not a vibe.

---

## 2. Current State

### 2.1 After SPEC-018, SPEC-019, SPEC-020

- Cook events persisted (`nutrition_cook_events`).
- Biometric observations and off-plan intakes persisted.
- Trajectory snapshots computed nightly.
- No component joins these into adherence metrics or drivers.
- No nightly materialization; every read would scan raw events.

### 2.2 Gaps

1. No canonical definition of "plan adherence", "target
   adherence", "goal progress" shipped anywhere.
2. No eaten rollup (SPEC-018 provides per-event nutrients; no
   per-day / per-week aggregation exists).
3. No drivers decomposition — gaps are numbers without named
   causes.
4. No feedback signal to SPEC-012 (adherence-derived preferences).

---

## 3. Goals and Non-Goals

### 3.1 Goals

- Ship `adherence.compute_window(client_id, start, end) ->
  AdherenceSnapshot` as the canonical computation.
- Materialize snapshots nightly (plus on-demand refresh with
  rate-limit) into `nutrition_adherence_snapshots`.
- Produce three clearly separated gauges per window: plan
  adherence, target adherence, goal progress status.
- Produce structured `drivers` that name the top contributors to
  any gap with quantified impact.
- Feed adherence-derived signals into SPEC-012's learned
  preferences (low weekday adherence → `prep_time.weekday` down,
  clustered ingredient swaps → negative ingredient affinity).
- Expose read APIs; no write APIs other than admin refresh.

### 3.2 Non-goals

- **No dashboard UI.** SPEC-022.
- **No recalibration.** SPEC-020 owns it; this spec is one of its
  inputs via the target-adherence signal.
- **No predictions or forecasts.** This is historical aggregation
  only.
- **No cross-user benchmarks.** Per-user.

---

## 4. Detailed Design

### 4.1 Module layout

```
backend/agents/nutrition_meal_planning_team/adherence/
├── __init__.py                 # compute_window, ADHERENCE_VERSION
├── version.py                  # ADHERENCE_VERSION = "1.0.0"
├── types.py                    # AdherenceSnapshot, DriverItem, Gauge
├── plan_adherence.py
├── target_adherence.py
├── goal_progress.py
├── drivers.py                  # decomposition logic
├── eaten_rollup.py             # cook events + off-plan intake → nutrients
├── preferences_feedback.py     # → SPEC-012
├── snapshots.py                # Postgres persistence + nightly job
├── errors.py
└── tests/
```

### 4.2 Types

```python
@dataclass(frozen=True)
class Gauge:
    value: float                 # 0.0..1.0 for adherence; -1..+1 for goal progress
    confidence: float
    status: Literal["on_track", "caution", "off_track", "insufficient_data"]
    explanation: str             # short NL for UI

@dataclass(frozen=True)
class DriverItem:
    kind: Literal[
        "skipped_meal", "swapped_meal", "macro_gap_day", "macro_over_day",
        "off_plan_intake", "low_log_rate", "plan_gap"
    ]
    contribution: float          # signed, kcal or g or mg depending on context
    unit: str
    detail: str                  # "Tuesday lunch skipped"
    recipe_id: Optional[str] = None
    observed_at: Optional[str] = None

@dataclass(frozen=True)
class AdherenceSnapshot:
    client_id: str
    window_start: date
    window_end: date
    plan_adherence: Gauge
    target_adherence: Gauge
    goal_progress: Gauge
    eaten_rollup: dict[Nutrient, float]        # window totals
    drivers: tuple[DriverItem, ...]
    inputs_hash: str                            # for idempotent materialization
    adherence_version: str
    computed_at: str
```

### 4.3 Plan adherence

Per-recipe-scheduled-in-window, compute:

- `made`: 1.0 weight; `servings_made >= portions * 0.75` → 1.0,
  else proportional.
- `partial`: `servings_made / portions_servings_numeric`, capped
  at 1.0.
- `swapped`: 0.8 weight (user followed the plan's intent but
  diverged on specifics).
- `skipped`: 0.0.
- Not yet logged (plan recipe whose mealtime has passed and no
  cook event exists): treated as `skipped` at snapshot time
  unless the user has enabled "don't penalize unlogged meals"
  (profile flag). Default is skip-penalty so adherence reflects
  reality; SPEC-022 nudges users to log.

`plan_adherence.value = Σ weights / count`. Confidence a function
of the total log rate across the window.

### 4.4 Eaten rollup

Pure function over SPEC-018 cook events + SPEC-019 off-plan
intakes within the window:

- Cook events contribute `effective_nutrients` (SPEC-018 §4.5) ×
  (servings_made or proportional).
- Off-plan intakes contribute `estimated_nutrients` (SPEC-019)
  with their confidence.
- Skipped recipes contribute zero.
- Net totals per day and per window.

Runs deterministically; cached by `(client_id, window)` with
TTL to next snapshot compute.

### 4.5 Target adherence

Compare eaten rollup vs. `DailyTargets` (SPEC-003 history —
targets may have changed mid-window due to SPEC-020 recalibration;
we look up the prevailing target per day, not the latest).

- Per-nutrient per-day variance from target.
- Per-nutrient weekly adequacy for the adequacy-list nutrients
  (fiber, iron, D, K, Ca, B12).
- Gauge value: mass-weighted fraction of nutrient-day-targets met
  within tolerance (SPEC-010 `Tolerances`).
- Confidence: lower when off-plan intake rate is high (intake
  estimates have baseline uncertainty).

### 4.6 Goal progress

For `weight_kg` (v1 only):

- Compare observed EMA delta over window vs. SPEC-020 expected
  trajectory delta.
- `value = 1 - |observed - expected| / tolerance` where
  `tolerance = max(0.5 kg, 0.3% body weight per week)`.
- Status mapping:
  - `on_track`: value ≥ 0.7
  - `caution`: 0.3 ≤ value < 0.7
  - `off_track`: value < 0.3
  - `insufficient_data`: fewer than 4 weight observations in window
- Cohorts where SPEC-020 returns `Trajectory=None` → goal_progress
  is `Gauge(status='insufficient_data')` with a clinical note
  from the cohort; never auto-fail.

### 4.7 Drivers decomposition

Named, quantified contributors are where the value of this spec
lives. For each gauge that is not `on_track`:

- **Plan adherence**: rank skipped/swapped meals by frequency and
  day. Surface top 3.
- **Target adherence**: for each out-of-tolerance nutrient:
  - Walk the window day by day.
  - Identify which days contributed the largest absolute delta
    toward the gap.
  - Attribute each day's delta to: (skipped planned recipe),
    (swapped for lower-nutrient substitute), (off-plan intake
    over/under the nutrient), or (plan itself was low on that
    nutrient — rare post-SPEC-010 repair).
  - Emit DriverItems sorted by absolute `contribution`.
- **Goal progress**: drivers are target adherence gap + plan
  adherence gap × expected-impact coefficient; the driver list
  references both.

Every driver has a quantified `contribution` and a human-readable
`detail`. No narrative-only drivers — the dashboard renders text,
but the spec's contract is quantified.

### 4.8 Preferences feedback (SPEC-012)

On every snapshot compute:

- **Skipped meal** on a weekday → negative contribution to
  `prep_time.weekday` (source=derived, evidence="skipped 3
  weekday dinners").
- **Swapped meal** with a substitution at the ingredient level →
  ingredient-affinity signals per SPEC-011's extractor call
  semantics (SPEC-018 cook-event path already emits them; this
  spec does not duplicate, only aggregates for the drivers view).
- **Consistent macro gap** (target adherence persistently low on
  protein, say) → advisory to planner prompt via SPEC-010 prompt
  variables, not SPEC-012 (because it is plan-shape, not user
  taste).

Fed via structured events on the existing event bus. Consumers
are SPEC-012's aggregator and SPEC-010's planner prompt.

### 4.9 Nightly materialization + on-demand

Scheduled task (Temporal):

- Runs nightly per active client.
- Windows materialized: last 7 days, last 30 days, current month.
- Rolling windows keyed by (client_id, window_label, window_end).
- Idempotent via `inputs_hash` — same inputs produce the same
  snapshot. Re-runs with identical input short-circuit.

On-demand refresh: `POST /adherence/{client_id}/refresh` (rate-
limited to 1 per 5 min per user). Used by the dashboard "refresh
now" action.

### 4.10 Persistence

Migration `018_adherence.sql`:

```sql
CREATE TABLE IF NOT EXISTS nutrition_adherence_snapshots (
    id                     BIGSERIAL PRIMARY KEY,
    client_id              TEXT NOT NULL REFERENCES nutrition_profiles(client_id)
                               ON DELETE CASCADE,
    window_label           TEXT NOT NULL,    -- 'last_7_days' | 'last_30_days' | 'month_2026_04' | ...
    window_start           DATE NOT NULL,
    window_end             DATE NOT NULL,
    plan_adherence_json    JSONB NOT NULL,
    target_adherence_json  JSONB NOT NULL,
    goal_progress_json     JSONB NOT NULL,
    eaten_rollup_json      JSONB NOT NULL,
    drivers_json           JSONB NOT NULL,
    inputs_hash            TEXT NOT NULL,
    adherence_version      TEXT NOT NULL,
    computed_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (client_id, window_label, window_start, window_end, inputs_hash)
);
CREATE INDEX ON nutrition_adherence_snapshots (client_id, window_end DESC);
```

Also writes `nutrition_adherence_events` referenced by SPEC-018
§4.6 — that table already exists in SPEC-018's migration as a
cook-event adherence projection; this spec reads it.

### 4.11 API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/adherence/{client_id}?window=last_7_days` | Latest snapshot for a window |
| `GET` | `/adherence/{client_id}/drivers?window=last_30_days` | Drivers only (lighter payload) |
| `POST` | `/adherence/{client_id}/refresh` | Force recompute, rate-limited |
| `GET` | `/adherence/{client_id}/eaten-rollup?window=...` | Eaten rollup payload (shared with SPEC-020 recalibration) |

Consumers:

- SPEC-022 dashboard: reads snapshots and drivers.
- SPEC-020 recalibration: reads eaten rollup and target
  adherence as inputs to its evaluator.
- SPEC-012 learned preferences: subscribes to the event stream.

### 4.12 Observability

- `adherence.snapshot_computed{window}`.
- `adherence.gauge_status{kind, status}` — counters keyed by
  plan/target/goal and status.
- `adherence.drivers_count_per_snapshot` histogram.
- `adherence.low_log_rate_detected`.
- `adherence.compute_latency_ms` histogram.
- Alert: `adherence.gauge_status{goal_progress, status=off_track}`
  rate rising across cohort → not necessarily an app issue but
  worth investigating (could be a calculator regression).

### 4.13 Privacy

- Snapshots are derived from PHI-adjacent data; same retention +
  cascade delete as SPEC-019.
- Driver `detail` strings contain recipe names and weekday
  references; no client_id leakage.
- Log redaction keeps driver detail strings out of INFO logs.

### 4.14 Priority-grouped work items

| # | Item | Priority |
|---|------|----------|
| W1 | Module scaffolding, version, types | P0 |
| W2 | Migration `018_adherence.sql` | P0 |
| W3 | `eaten_rollup.py` + tests | P0 |
| W4 | `plan_adherence.py` with weighting rules + tests | P0 |
| W5 | `target_adherence.py` with target history lookup + tolerance application + tests | P0 |
| W6 | `goal_progress.py` consuming SPEC-020 trajectory + tests | P0 |
| W7 | `drivers.py` decomposition + ranked emission + tests | P0 |
| W8 | Snapshot materialization: nightly job + idempotent hash | P0 |
| W9 | `/adherence/{client_id}` endpoints | P0 |
| W10 | `preferences_feedback.py` event emission to SPEC-012 | P1 |
| W11 | On-demand refresh (rate-limited) | P1 |
| W12 | Observability counters + alerting | P1 |
| W13 | Benchmarks: snapshot compute p99 ≤ 500 ms for 30-day window; read p99 ≤ 80 ms | P2 |

---

## 5. Rollout Plan

Flag `NUTRITION_ADHERENCE` (off → no snapshots, no endpoints;
on → nightly job + endpoints).

### Phase 0 — Foundation (P0)
- [ ] SPEC-018, SPEC-019, SPEC-020 at 100% ramp.
- [ ] W1–W8 landed. Migration applied in staging.

### Phase 1 — Shadow snapshots (P0)
- [ ] Flag on internal. Nightly job runs; snapshots persist.
- [ ] Team reviews 10 internal snapshots manually.
- [ ] Acceptance: gauges match the reviewer's intuitive read of
      the user's week in ≥8 of 10 cases; drivers name the
      actual top contributors.

### Phase 2 — Endpoints + feedback (P0/P1)
- [ ] W9, W10, W11 landed.
- [ ] Preferences-feedback events observed in SPEC-012's
      aggregator logs; preference digests updated when signals
      flow.

### Phase 3 — Ramp (P1)
- [ ] 10% → 50% → 100% over two weeks.
- [ ] Monitor: snapshot compute latency, gauge-status
      distribution, driver-count distribution, rate of
      `insufficient_data` (if high, logging-nudge work is
      warranted).

### Phase 4 — Cleanup (P1/P2)
- [ ] W12, W13 landed.
- [ ] Flag default on; removal scheduled.

### Rollback
- Flag off → snapshots stop; persisted rows retained (inert).
- Additive migration.

---

## 6. Verification

### 6.1 Unit tests

- `test_plan_adherence_weighting.py` — made/partial/swapped/skipped
  produce the documented weights; unlogged past-mealtime recipe
  counts as skipped (default) or ignored (flag).
- `test_eaten_rollup_composition.py` — cook events + off-plan
  intakes sum correctly; overlapping off-plan and made event
  handled (not double-counted).
- `test_target_adherence_midwindow_target_change.py` — SPEC-020
  recalibration acceptance mid-window: the correct per-day
  target applies to each day.
- `test_goal_progress_tolerance.py` — weight changes within
  tolerance produce `on_track`; outside produce `caution` or
  `off_track`.
- `test_driver_ranking.py` — synthetic gap (skipped Tuesday +
  Thursday lunches) produces a `skipped_meal` driver with
  correct contribution.
- `test_insufficient_data.py` — <4 weight observations → goal
  progress `insufficient_data`.

### 6.2 Integration tests

- `test_snapshot_nightly_idempotent.py` — same inputs → same
  `inputs_hash` → unique constraint skips duplicate write.
- `test_preferences_feedback_emitted.py` — low weekday
  adherence causes the expected SPEC-012 signal to land.
- `test_adherence_api_consistency.py` — endpoint returns
  snapshot identical to what the nightly job stored.
- `test_refresh_rate_limit.py` — 2 refreshes within 5 min →
  second returns 429.

### 6.3 Reviewer audit (Phase 1)

- 10 internal snapshots reviewed. Gauges and top-3 drivers match
  reviewer intuition in ≥8/10.

### 6.4 Property tests

- Determinism: replay a week of events → byte-equal snapshot.
- Monotonicity: adding more `made` cook events within a window
  never decreases plan adherence.
- Driver sum sanity: sum of `contribution` values across
  macro-gap drivers roughly equals the gauge's absolute gap
  (not strict equality because of driver categorization overlap,
  but within 20%).

### 6.5 Observability

All §4.12 counters emit.

### 6.6 Cutover criteria

- All P0 tests green.
- Phase 1 reviewer acceptance met.
- Phase 3 ramp: snapshot latency within budget; no correctness
  incidents.

---

## 7. Open Questions

- **Default penalty for unlogged past-mealtime recipes.** Default
  `skip-penalty` makes adherence numbers conservative. Some
  users will find this frustrating. The profile toggle
  "don't penalize unlogged meals" is a safety valve. We may
  change the default after Phase 3 data.
- **Driver categorization.** The categories in §4.2 are closed.
  Adding one is an additive change; removing or renaming is a
  major version bump.
- **Month-over-month windows.** `month_YYYY_MM` works for the
  current month; backfill of prior months is computed on-demand
  and cached. Deep history beyond 24 months may be expensive to
  compute; we accept that historical deep queries have longer
  latency than recent.
- **"Insufficient data" threshold for weight observations.** Four
  observations in a window is a low bar; some users weigh
  weekly. SPEC-022 will nudge log cadence; insufficient_data is
  legitimate, not a failure.
