# SPEC-022: Goal-progress dashboard

| Field       | Value                                                    |
|-------------|----------------------------------------------------------|
| **Status**  | Proposed                                                 |
| **Author**  | Nutrition & Meal Planning team                           |
| **Created** | 2026-04-17                                               |
| **Priority**| P1 (capstone — the user-visible surface for ADR-006)     |
| **Scope**   | Dashboard API aggregator, `user-interface/` dashboard view, weekly-weigh nudge UX, safety-state banner, recalibration accept/decline surface, copy review and a11y |
| **Depends on** | SPEC-019 (observations + off-plan log), SPEC-020 (trajectory + recalibration), SPEC-021 (adherence + drivers), SPEC-018 (cook events) |
| **Implements** | ADR-006 §9 (dashboard API + view), all §6 safety rails on the UI side |

---

## 1. Problem Statement

After SPEC-019 through SPEC-021, the team has every input ADR-006
asked for. Nothing renders them to the user.

This spec ships the dashboard: the user-visible page that answers
"is this working?" across the three separated gauges (plan
adherence, target adherence, goal progress), surfaces the structured
drivers as actionable bullets, renders the expected-vs-observed
trajectory, hosts the recalibration accept/decline surface, and
enforces the safety rails on the UI — including disabling the
scale-centric view when the ED-history flag is on.

It is the highest-stakes UI the team owns. A single careless
design decision (shame-framed copy, surprise scale chart, daily-
weigh nudge) does real user harm. This spec treats that seriously
and encodes the rules in §6 (copy, §7 accessibility, and §8 rail
enforcement.

It ships exactly one new backend endpoint — a thin aggregator
that combines snapshots for fast render — and the UI proper. No
new data produced here; everything is a read.

---

## 2. Current State

### 2.1 After SPEC-019–SPEC-021

- Observations, cook events, off-plan intakes, adherence
  snapshots, trajectory snapshots, recalibration proposals all
  exist and are individually queryable.
- No unified dashboard endpoint.
- No dashboard UI.
- Recalibration proposals live in the database but have nowhere
  to be accepted from.

### 2.2 Gaps

1. No `DashboardView` payload combining the above for a single
   page render.
2. No safety-state computed gauge for the UI.
3. No ED-flag-aware view rendering.
4. No weekly-weigh cadence nudge (ADR-006 §6 explicitly resists
   daily-weigh UX).
5. No accept/decline UX for recalibration.

---

## 3. Goals and Non-Goals

### 3.1 Goals

- Ship `GET /dashboard/{client_id}?period=week|month|quarter`
  returning a `DashboardView` the UI renders in one request.
- Ship the dashboard view: three gauges, drivers, trajectory
  chart, eaten-rollup summary, recalibration surface, safety
  state banner.
- Replace scale-centric components with behavior-centric ones
  when `ed_history_flag=true`.
- Weekly weigh cadence: the UI invites one weigh per 7 days at
  most, never prompts for daily. 7-day EMA is the primary chart
  series.
- Copy and UX adhere to ADR-006 §6.5 rules: behavior- and
  energy-framed, never shame-framed. Shipped copy reviewed.
- Accessibility: AA contrast, screen-reader labels on gauges,
  keyboard navigable.
- No surprises: any automatic state change (proposal auto-
  expiring, rate-cap kicking in) is explained, not hidden.

### 3.2 Non-goals

- **No new data.** This spec only reads.
- **No exports.** Clinician PDF/CSV export of adherence and
  observations is v1.1.
- **No device connect flows in this spec.** SPEC-019's device-
  adapter follow-ups own that.
- **No in-dashboard editing of plan or targets.** The dashboard
  links to the relevant edit surfaces; it does not own them.
- **No comparison across users.** Per-user only.

---

## 4. Detailed Design

### 4.1 Module layout

```
backend/agents/nutrition_meal_planning_team/dashboard/
├── __init__.py
├── types.py               # DashboardView
├── aggregator.py          # fan out to other stores + compose
├── safety.py              # compute safety_state for UI
├── errors.py
└── tests/
```

UI changes primarily in `user-interface/src/app/components/nutrition/dashboard/`.

### 4.2 `DashboardView` type

```python
class DashboardView(BaseModel):
    client_id: str
    period: Literal["week", "month", "quarter"]
    generated_at: str

    # Three gauges (from SPEC-021)
    plan_adherence: Gauge
    target_adherence: Gauge
    goal_progress: Gauge

    # Trajectory (from SPEC-020)
    trajectory: Optional[TrajectorySeries]      # None for unsupported cohorts
    observed_series: List[ObservationPoint]     # 7-day EMA
    last_weigh_days_ago: Optional[int]
    weigh_nudge: Optional[str]                  # "It's been 10 days since your last weigh-in."

    # Eaten rollup (from SPEC-021)
    eaten_rollup: Dict[Nutrient, float]

    # Drivers (from SPEC-021)
    drivers: List[DriverItem]                   # top 5 by contribution

    # Recalibration (from SPEC-020)
    recalibration_proposal: Optional[RecalibrationProposal]

    # Safety state
    safety_state: SafetyState
    cohort: PlanCohort                          # informs rendering
    view_variant: Literal["standard", "ed_adjacent", "minor", "pregnancy_lactation", "clinician_guided"]
```

### 4.3 Safety state

```python
class SafetyState(BaseModel):
    level: Literal["ok", "caution", "rail_active"]
    active_rails: List[str]                     # ['rate_of_loss', 'bmi_floor']
    message: Optional[str]                      # user-facing text
    action_cta: Optional[str]                   # e.g. "Review your goal"
```

Computed in `safety.py` from SPEC-020's rail event log + current
profile flags. Surfaced prominently in the dashboard header when
non-ok. `rail_active` pre-empts normal gauge display with a
clear, non-alarming banner.

### 4.4 View variants

- **standard**: three gauges, trajectory, drivers, recalibration.
- **ed_adjacent** (`ed_history_flag=true` OR cohort=ed_adjacent):
  - No trajectory chart.
  - No weight observations shown (even if logged).
  - Goal progress gauge replaced with "variety" +
    "cook streak" + "plan follow-through" behavior metrics.
  - Eaten-rollup shown as **food-group balance** (servings of
    veg, whole grains, protein sources) not as kcal numbers.
  - No recalibration surface.
- **minor** (age_years < 18):
  - No trajectory.
  - Goal progress replaced with "variety" and "plan
    follow-through".
  - Prominent "growth-focused guidance" note.
- **pregnancy_lactation** / **clinician_guided**: scalar gauges
  hidden; nutrient-summary + cook-streak only; "work with your
  clinician" note from the SPEC-020 trajectory response.

Variants are selected server-side in the aggregator. The UI must
not attempt to "upgrade" a variant to standard.

### 4.5 Weekly-weigh nudge

- `last_weigh_days_ago` populated from SPEC-019.
- UI rule: if `last_weigh_days_ago >= 7`, show a single gentle
  nudge on the dashboard: *"It's been 10 days — want to log a
  weigh-in?"* with a one-click log action.
- If `last_weigh_days_ago < 7`, no nudge. We explicitly do not
  encourage daily weighing.
- Copy exception: if `ed_adjacent` variant, the nudge is
  suppressed entirely.

### 4.6 Trajectory chart

- Line chart: expected band (shaded) + observed 7-day EMA line.
- X axis: date; Y axis: weight (kg or lb per profile unit
  preference).
- If last observation is > 14 days old, the EMA line dashes out
  past that point.
- Hover / focus on a point shows the numeric value and date; no
  surprise colors.
- Unsupported cohort: chart omitted and replaced with a neutral
  note.

### 4.7 Drivers UI

Each driver rendered as a single bullet:

```
• Three weekday dinners skipped
    → about 18 g/day of your protein gap this week
    [Adjust cook reminders]
```

Quantified impact visible. CTA links (where available) deep-link
to the relevant edit surface (cook-mode reminders, plan
regeneration, pantry import, etc.). If no CTA is appropriate,
the bullet is informational only.

### 4.8 Recalibration surface

- When a `RecalibrationProposal` is present:
  - Card on the dashboard: *"Your maintenance looks closer to
    2,250 kcal than 2,400. Here's why: [brief
    explanation.] [Review proposal]"*
  - "Review" opens a modal with inputs used, window, confidence,
    impact on next plan. Two buttons: "Accept" and "Not now".
  - Accepted proposals persist the adjustment (SPEC-020 §4.4);
    a confirmation toast appears on the next plan generation.
  - Declined proposals are hidden until the 21-day anti-thrash
    window elapses.

- When no proposal: no card.

### 4.9 Aggregator endpoint

`GET /dashboard/{client_id}?period=week` composes:

1. `adherence.compute_window` → latest materialized snapshot
   (SPEC-021).
2. `biometrics.get_series` → 7-day EMA (SPEC-019).
3. `biometrics.get_latest` → `last_weigh_days_ago`.
4. `trajectory.get_latest` → expected series (SPEC-020).
5. `recalibration.get_active_proposal` → latest proposal.
6. `safety.compute_safety_state` → UI state.
7. Profile → cohort + variant selection.

All subcalls run concurrently; snapshot reads are sub-10 ms,
trajectory read sub-50 ms. Endpoint p99 budget: ≤ 150 ms.

Refresh: dashboard does not auto-refresh more than once per open.
An explicit "Refresh now" button calls
`POST /adherence/{client_id}/refresh` (rate-limited per SPEC-021).

### 4.10 UI states enumerated

- **Loading**: skeleton render of the three gauges + chart.
- **First-time user** (cold-start): three gauges read
  `insufficient_data`; chart shows "add your weight to see your
  trajectory"; drivers list shows "we'll show what's driving
  your progress once there's a few days of data."
- **No goal set**: hide goal_progress gauge and trajectory; show
  "set a goal" CTA linking to the profile.
- **Unsupported cohort**: prominent neutral note explaining why
  some views are not shown; link to clinician-consult guidance.
- **Safety rail active** (e.g. rate_of_loss): banner at the top
  reading *"We've paused your weight-loss goal because your
  trend is moving faster than is safe. Your plan will focus on
  maintenance until you ease off."* with a "Why?" link to
  details.
- **Recalibration pending**: proposal card above the gauges.

### 4.11 Accessibility

- WCAG AA color contrast for all gauge, chart, and status colors.
- Screen-reader labels on every gauge: *"Plan adherence: 85
  percent, on track."*
- Charts are keyboard-navigable; arrow-key focus on data points
  with announced values.
- No information conveyed solely by color; status chips have
  text labels.
- `prefers-reduced-motion` disables gauge animation.

### 4.12 Copy governance

- Every user-facing string in the dashboard is declared in a
  single i18n-ready `strings.ts` (Angular) with a comment tag
  linking to the ADR-006 §6.5 rule it satisfies
  (behavior-framed, no shame, etc.).
- Every string added or changed requires a reviewer from the
  dedicated copy-review list (team lead + one external reviewer
  initially).
- `strings.ts` is checked into the repo; PRs modifying it
  require the copy reviewer on CODEOWNERS.

### 4.13 Observability

- `dashboard.viewed{variant}`.
- `dashboard.gauge_status_rendered{kind, status}`.
- `dashboard.recalibration_card_{viewed, accepted, declined}`.
- `dashboard.safety_banner_viewed{rail}`.
- `dashboard.weigh_nudge_{shown, acted}`.
- `dashboard.latency_ms` histogram.
- Alerts: `dashboard.safety_banner_viewed{rail=bmi_floor}` rate
  rising → on-call review (not an incident; a signal).

### 4.14 Privacy and data display

- Dashboard renders sensitive data. The URL route requires
  authenticated session; no share links or deep-link
  enumeration.
- Logs never include dashboard payloads above DEBUG.
- Server-side rendering (if added later) has to respect the same
  redaction rules; v1 is client-side only.

### 4.15 Priority-grouped work items

| # | Item | Priority |
|---|------|----------|
| W1 | `DashboardView` type + aggregator endpoint | P0 |
| W2 | `safety.compute_safety_state` + unit tests | P0 |
| W3 | View-variant selection logic | P0 |
| W4 | UI: standard view (three gauges + drivers list) | FE | P0 |
| W5 | UI: trajectory chart + observed EMA | FE | P0 |
| W6 | UI: recalibration card + accept/decline modal | FE | P0 |
| W7 | UI: safety banner (rate_of_loss, bmi_floor, ed filters) | FE | P0 |
| W8 | UI: ED-adjacent / minor / cohort variant screens | FE | P0 |
| W9 | UI: weekly-weigh nudge | FE | P1 |
| W10 | Copy file + copy review process + CODEOWNERS entry | P0 |
| W11 | Accessibility pass (a11y audit + fixes) | P0 |
| W12 | UI: empty / cold-start states | FE | P1 |
| W13 | Observability counters + alerting | P1 |
| W14 | Benchmarks: endpoint p99 ≤ 150 ms; UI first-render ≤ 500 ms | P2 |
| W15 | i18n-readiness of the copy file | P2 |

---

## 5. Rollout Plan

Flag `NUTRITION_DASHBOARD` (off → dashboard route 404; on →
available).

### Phase 0 — Foundation (P0)
- [ ] SPEC-019, SPEC-020, SPEC-021 at 100% ramp.
- [ ] W1–W3 landed.
- [ ] Copy written and reviewed (W10).

### Phase 1 — Internal dogfood (P0)
- [ ] W4–W8 landed.
- [ ] Flag on internal. Team uses the dashboard on real profiles
      for 2 weeks.
- [ ] Acceptance gates:
      - Zero shame-framed copy incidents.
      - Zero safety rail UI errors (banner shown but rail not
        active, or vice versa).
      - `ed_adjacent` variant hides all scale-centric elements
        on an internal dogfood profile.

### Phase 2 — A11y + polish (P0/P1)
- [ ] W11 a11y audit; findings resolved.
- [ ] W9 nudge; W12 cold-start; W13 observability.
- [ ] External copy reviewer sign-off on every visible string.

### Phase 3 — Ramp (P1)
- [ ] 10% → 25% → 50% → 100% over three weeks.
- [ ] Watch metrics:
      - dashboard.viewed per active user per week.
      - Recalibration acceptance rate.
      - Weigh nudge action rate.
      - Support tickets mentioning the dashboard — triage for
        any surprise or shame-framed report.

### Phase 4 — Cleanup (P1/P2)
- [ ] W14 benchmarks; W15 i18n.
- [ ] Flag default on; removal scheduled.

### Rollback
- Flag off → dashboard route hidden; underlying data untouched.
- No migration.

---

## 6. Verification

### 6.1 Unit tests

- `test_variant_selection.py` — matrix of profile states →
  correct variant.
- `test_safety_state.py` — each rail combination produces the
  expected state + message.
- `test_aggregator.py` — concurrent fan-out returns a consistent
  snapshot; partial failures (e.g. trajectory unavailable)
  degrade to `insufficient_data` not 500.

### 6.2 Integration tests

- `test_dashboard_standard.py` — standard profile at 100%
  adherence renders all components correctly.
- `test_dashboard_ed_flag.py` — `ed_history_flag=true` profile
  renders `ed_adjacent` variant; scale-centric elements absent
  from the payload (not just hidden in CSS).
- `test_dashboard_minor.py` — age<18 renders minor variant.
- `test_dashboard_pregnancy.py` — pregnancy cohort renders
  pregnancy_lactation variant.
- `test_dashboard_safety_rail_active.py` — rate_of_loss rail
  active → banner rendered + plan generation clamped (SPEC-020
  integration).
- `test_recalibration_accept_flow.py` — proposal accept →
  profile `tdee_adjustment_kcal` updated → dashboard reflects
  "accepted" state.

### 6.3 UX + copy audit (Phase 1/2)

- External copy reviewer audits every string against ADR-006
  §6.5 rules. Findings:
  - Zero shame-framed strings.
  - Zero strings that imply user failure.
  - All safety-state strings reviewed with heightened care.

### 6.4 Accessibility audit (Phase 2)

- `axe-core` or equivalent across the dashboard; zero AA
  violations.
- Manual screen-reader pass confirms gauges announce values and
  status; trajectory chart points navigable.
- `prefers-reduced-motion` respected.

### 6.5 Property tests

- Variant selection deterministic.
- Any profile with `ed_history_flag=true` under any cohort
  produces a variant that omits scale / calorie numbers from
  the payload. Enforced in the aggregator, not just the UI.

### 6.6 Observability

All §4.13 counters emit.

### 6.7 Cutover criteria

- All P0/P1 tests green.
- Copy + a11y audits passed.
- Phase 1 dogfood acceptance met.
- Phase 3 ramp: zero safety-rail UI bugs; acceptance rate of
  recalibration in a reasonable range (not 0%, not 100%).
- Clinical + legal review sign-off (dashboard is the most
  likely surface to attract review).

---

## 7. Open Questions

- **Daily weight logging opt-in.** Some users insist on daily
  weighing. v1 resists via nudge cadence; if enough users
  request it, a user-opt-in "I want daily prompts" toggle can
  be added — but the default never changes.
- **Goal-progress tolerance visualization.** Showing the
  trajectory band fills this role. We may also want a small
  gauge labeled with an interpretation ("~0.3 kg/week loss,
  within your goal of 0.45"). Nice-to-have; revisit Phase 3.
- **Clinician dashboard export.** Clipped PDF/CSV for sharing
  with a dietitian or PCP. v1.1.
- **"Why are my numbers stuck?" explanation depth.** Drivers
  tell the immediate story. A deeper diagnostic ("your cooking
  times have crept up, your weekend adherence is low") lives
  in the drivers categorization. If user-testing indicates the
  current drivers are too concise, expand; not speculatively.
- **Mobile-optimized layout.** The dashboard is dense. v1 is
  web-first; mobile-web is styled but not reflowed per-
  component. Dedicated mobile-native layout is a future spec.
