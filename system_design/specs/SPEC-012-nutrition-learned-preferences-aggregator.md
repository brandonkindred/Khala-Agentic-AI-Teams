# SPEC-012: Learned preferences aggregator and digest

| Field       | Value                                                    |
|-------------|----------------------------------------------------------|
| **Status**  | Proposed                                                 |
| **Author**  | Nutrition & Meal Planning team                           |
| **Created** | 2026-04-17                                               |
| **Priority**| P0 (blocks SPEC-013 retrieval; consumed by planner prompt update) |
| **Scope**   | New module `backend/agents/nutrition_meal_planning_team/preferences/aggregator/`, Postgres additions, planner prompt integration, user-facing preference endpoints, admin override path |
| **Depends on** | SPEC-011 (signal extraction) |
| **Implements** | ADR-004 §1 (dimensions), §3 (update rule), §4 (planner integration), §6 (user-visible preference panel) |

---

## 1. Problem Statement

SPEC-011 ships the extraction layer: every feedback event produces a
`FeedbackSignals` record. This spec converts that stream into a
stable, per-user `LearnedPreferences` record, exposes it to the
planner as a digest, and makes it visible and editable by the user.

The thesis of ADR-004 is that the signal per feedback event is weak
because the aggregation is primitive. This spec is where the
aggregation lives: a Bayesian-smoothed EWMA per (dimension, key),
a minimum-evidence gate, per-profile overrides, and a compact
digest shape the planner can prompt against.

It is also the first spec that users see from this workstream. The
"what we've learned about you" panel is a product-differentiating
surface and is specified here so the contract between backend and
UI is explicit.

---

## 2. Current State

### 2.1 After SPEC-011

- Every feedback event yields a `FeedbackSignals` row in
  `nutrition_feedback_signals` with structured signals.
- No aggregation. Planner still reads the legacy name-list
  summarizer.

### 2.2 Gaps

1. No per-user rollup; signals accumulate but never consolidate.
2. No time-weighting — old feedback treated the same as new.
3. No minimum-evidence gate; a single strong note could dominate a
   dimension.
4. No planner-visible digest shape.
5. No UI for users to see or correct what was learned.
6. No override surface for user corrections or clinician direction.

---

## 3. Goals and Non-Goals

### 3.1 Goals

- Ship a deterministic aggregator that turns the
  `nutrition_feedback_signals` stream into a per-user
  `LearnedPreferences` record.
- Use a Bayesian-smoothed EWMA per (dimension, key) with explicit
  decay, source weighting, and a minimum-evidence threshold.
- Produce a compact `PreferenceDigest` for the planner (top-K
  positive, top-K negative signals; short NL summary; exemplar
  meal-name lists).
- Expose GET/PUT/DELETE endpoints for users to inspect, pin,
  exclude, or forget learned signals. Overrides always beat
  learned values.
- Update the meal-planner prompt to consume the digest instead of
  the legacy name list.
- Version the aggregator; keep schema migrations additive.

### 3.2 Non-goals

- **No retrieval or embeddings.** SPEC-013 covers similar-past-hit
  retrieval and few-shot exemplar injection.
- **No cross-user signal sharing.** Per-user only.
- **No learning from cooking events.** Cooking events (ADR-005)
  feed this aggregator through the same `FeedbackSignals` schema
  when they land; the aggregator needs no change.
- **No onboarding quiz.** Cold-start seeding via a quiz is listed
  as v1.1 in ADR-004 §7 and is a separate spec.

---

## 4. Detailed Design

### 4.1 Module layout

```
backend/agents/nutrition_meal_planning_team/preferences/aggregator/
├── __init__.py            # update_preferences, build_digest, AGGREGATOR_VERSION
├── version.py             # AGGREGATOR_VERSION = "1.0.0"
├── types.py               # LearnedPreferences, DimensionState, Score, PreferenceDigest
├── update.py              # Bayesian EWMA + source weighting
├── digest.py              # compute digest for planner
├── overrides.py           # pin / exclude / forget merge logic
├── errors.py
└── tests/
```

### 4.2 Data shape

```python
@dataclass(frozen=True)
class Score:
    mean: float            # posterior mean in [-1.0, +1.0]
    confidence: float      # 0..1 derived from pseudo-counts
    evidence_count: int    # number of supporting feedback events
    last_updated: str      # ISO

@dataclass(frozen=True)
class DimensionState:
    scores: dict[str, Score]       # key -> Score
    last_updated: str

@dataclass(frozen=True)
class Override:
    action: Literal["pin_positive", "pin_negative", "exclude", "forget"]
    value: Optional[float] = None  # pinned score if action=pin_*
    reason: Optional[str] = None   # user-provided
    author: str = "user"           # "user" | "clinician"
    recorded_at: str = ""

@dataclass(frozen=True)
class LearnedPreferences:
    client_id: str
    dimensions: dict[Dimension, DimensionState]
    overrides: dict[tuple[Dimension, str], Override]
    aggregator_version: str
    last_extraction_id: Optional[str]   # for idempotent resume
```

### 4.3 Update rule

For each new `FeedbackSignals`:

1. Decay current scores for each `(dimension, key)` by the age of
   the last update:
   ```
   decayed_mean = current_mean * exp(-lambda * days_since_last)
   decayed_alpha = current_alpha * exp(-lambda * days_since_last)
   decayed_beta  = current_beta  * exp(-lambda * days_since_last)
   ```
   where `lambda = 0.02` per day (≈ half-life of ~35 days). The
   decay acts on the pseudo-counts so confidence also ages.
2. For each incoming signal, compute its effective weight:
   ```
   w = base_weight(source) * confidence
     * (1 if source=note else 0.6)        # note signals weigh more
     * (rating_amplifier)                 # up to 1.3x for 5★ + strong note
   ```
3. Update pseudo-counts using a Beta-Bernoulli-style update mapped
   to a signed strength: positive contributions add to `alpha`,
   negative to `beta`; magnitude scales both.
4. Recompute posterior mean = `alpha / (alpha + beta)` rescaled to
   `[-1, +1]`; confidence = `1 - 1/(alpha + beta + 1)`.
5. Apply minimum-evidence gate: dimension/key with
   `evidence_count < N_MIN` (default 3) is hidden from the digest
   and the UI even if `confidence` is high. Stored anyway — we
   surface it once evidence accumulates.

All constants live in `update.py` and are tunable via a small YAML
(`aggregator_config.yaml`) that is version-tagged.

### 4.4 Override behavior

Overrides are authoritative and always win:

- `pin_positive` / `pin_negative` → digest uses the pinned value;
  learned score is still updated in the background but not surfaced.
- `exclude` → this (dimension, key) never appears in the digest or
  planner prompt; still stored for transparency in the UI panel.
- `forget` → erase both learned score and any prior override for
  this (dimension, key). Required for privacy ("I corrected your
  wrong inference and want it gone"). Writes to an audit log; next
  feedback event can re-learn.

### 4.5 Digest (`digest.py`)

```python
@dataclass(frozen=True)
class PreferenceDigest:
    top_positive: List[DigestItem]     # length <= K (default 6)
    top_negative: List[DigestItem]     # length <= K
    summary_nl: str                    # 1–3 sentences, LLM-authored; cached
    exemplar_hits: List[str]           # meal names (top 5 highest-rated past meals)
    exemplar_misses: List[str]         # meal names (3 recent low-rated)
    digest_version: str
    built_at: str

@dataclass(frozen=True)
class DigestItem:
    dimension: Dimension
    key: str
    strength: float                    # posterior mean (or pinned)
    confidence: float
    source_note: Optional[str] = None  # "pinned" | "learned from N feedbacks" | "excluded"
```

Rules:

- Selection: sort by `|strength| * confidence` and take top-K per
  sign, excluding `exclude`-overridden entries.
- The NL summary is generated by a lightweight LLM call when the
  digest is built; cached on `LearnedPreferences` and refreshed
  only when the top-K items change. Budget: ≤ 1 LLM call per user
  per week on typical feedback cadence.
- Exemplar lists come from `meal_feedback_store` ordered by rating;
  exemplars are names only (no ingredients) in the digest —
  retrieval of similar-past-hits is SPEC-013's job.
- If fewer than `N_MIN` data points exist across the whole user
  (cold start), `top_positive` and `top_negative` are empty and
  `summary_nl` says so politely: "We're still learning what you
  like."

### 4.6 Planner integration

`meal_planning_agent` prompt (post-SPEC-007, post-SPEC-010) is
extended:

- Legacy name-list summarizer is removed.
- The prompt receives `PreferenceDigest` rendered as:
  ```
  What we've learned about this user (from 23 feedback events):
    Leans toward: one-pan meals, chicken or legumes, umami + herbaceous
    flavors, Mediterranean cuisine.
    Leans away from: long weeknight prep, raw tofu, heavy cream sauces.
    Past 5 hits: Chicken shawarma bowl, Shakshuka, Sheet-pan gnocchi, ...
    Recent misses: Raw-tofu poke, Leek & potato soup.
    User pins: avoid cilantro (user-set override).
  ```
- Exclusions from the override list appear as explicit "never
  include" lines, not as soft preferences.
- If the digest is empty (cold start), the prompt falls back to
  profile-only behavior — exactly today's pre-SPEC-012 behavior
  minus the legacy name-list noise.

Digest shape is stable; prompt wording is not, so tweaks can land
without changing the contract.

### 4.7 Storage

Migration `008_learned_preferences.sql`:

```sql
CREATE TABLE IF NOT EXISTS nutrition_learned_preferences (
    client_id          TEXT PRIMARY KEY
        REFERENCES nutrition_profiles(client_id) ON DELETE CASCADE,
    dimensions_json    JSONB NOT NULL,
    overrides_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
    digest_json        JSONB,
    aggregator_version TEXT NOT NULL,
    last_extraction_id TEXT,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nutrition_preference_override_log (
    id               BIGSERIAL PRIMARY KEY,
    client_id        TEXT NOT NULL,
    dimension        TEXT NOT NULL,
    key              TEXT NOT NULL,
    action           TEXT NOT NULL,
    value            DOUBLE PRECISION,
    reason           TEXT,
    author           TEXT NOT NULL,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Aggregation runs as a background task triggered by each successful
`nutrition_feedback_signals` insert. Idempotent: `last_extraction_id`
on the preferences row prevents double-application if the task
retries.

### 4.8 API

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/preferences/{client_id}` | Full `LearnedPreferences` + `PreferenceDigest` |
| `GET`  | `/preferences/{client_id}/digest` | Digest only (lightweight; used by UI and planner internals) |
| `PUT`  | `/preferences/{client_id}/override` | Body: `{dimension, key, action, value?, reason?}` |
| `DELETE` | `/preferences/{client_id}/signal/{dimension}/{key}` | Forget a single learned point (writes `forget` override) |
| `POST` | `/preferences/{client_id}/rebuild` | Admin-only. Rebuilds from `nutrition_feedback_signals` log. Used on `AGGREGATOR_VERSION` bump |

Override writes flush the cached `digest_json` and trigger a digest
recompute; the planner sees the new digest on the next plan.

### 4.9 UI — "What we've learned about you"

- Dedicated section on the profile page, or a slide-in panel
  accessible from the plan screen.
- Chips grouped by dimension:
  ```
  Loves:   [One-pan (8 feedbacks)]  [Chicken (5)]  [Umami (6)]  ...
  Avoids:  [Long weeknight prep (4)]  [Raw tofu (3)]  [Cream sauces (3)]
  Pinned:  [Cilantro — never include (you asked)]
  ```
- Each chip has actions: "That's right", "Not really — forget
  this", "Pin it stronger".
- Explanations under each chip: short provenance string (mined
  from the highest-confidence supporting feedback note, capped at
  140 chars). Uses the stored `evidence` snippets from SPEC-011.
- Copy is neutral and affirmative; never shame-framed. Reviewed
  against the ADR-006 §6.5 copy checklist.

### 4.10 Clinician authoring

Overrides can be written with `author=clinician` via an admin-only
path (same one SPEC-002 uses for `clinician_overrides`). Clinician
overrides are visually distinguished in the UI (subtle lock icon)
and cannot be removed by the user — only replaced by a newer
clinician write or a `forget` with admin auth.

### 4.11 Observability

- `preferences.aggregator.apply{outcome}` where outcome ∈
  `applied | idempotent | failed`.
- `preferences.aggregator.digest_recompute`.
- `preferences.aggregator.decay_runs`.
- `preferences.aggregator.dimensions_gated_by_n_min`.
- `preferences.aggregator.override_written{action, author}`.
- Latency histogram for update and digest computation.

### 4.12 Versioning

`AGGREGATOR_VERSION = "MAJOR.MINOR.PATCH"`:

- **MAJOR** — update rule or decay semantics change.
- **MINOR** — source-weight tuning, new source types.
- **PATCH** — non-behavioral.

On MAJOR bump, admin triggers `POST /preferences/{id}/rebuild`
which re-runs aggregation from the underlying
`nutrition_feedback_signals` stream. Minor bumps do not require
rebuild; changes apply incrementally on next event.

### 4.13 Priority-grouped work items

| # | Item | Priority |
|---|------|----------|
| W1 | Module scaffolding, version, types | P0 |
| W2 | Migration `008_learned_preferences.sql` + schema registration | P0 |
| W3 | `update.py` Bayesian EWMA + source weighting + decay; unit tests | P0 |
| W4 | `overrides.py` merge semantics + forget/pin/exclude logic | P0 |
| W5 | Aggregation background task on `nutrition_feedback_signals` insert; idempotent via `last_extraction_id` | P0 |
| W6 | `digest.py` top-K selection + empty cold-start behavior | P0 |
| W7 | LLM NL-summary generation + cache invalidation | P1 |
| W8 | API endpoints (§4.8) | P0 |
| W9 | Planner prompt integration (digest → prompt text) | P0 |
| W10 | UI panel: chips + provenance + actions | FE | P1 |
| W11 | Clinician override admin path | P1 |
| W12 | Observability counters + cold-start dashboard | P1 |
| W13 | Rebuild CLI + admin endpoint | P2 |
| W14 | Benchmarks: aggregation p99 ≤ 100 ms per event; digest build ≤ 200 ms | P2 |

---

## 5. Rollout Plan

Flag `NUTRITION_LEARNED_PREFERENCES` (off → legacy name-list path,
on → digest path). Planner prompt variant is switched by flag.

### Phase 0 — Foundation (P0)
- [ ] SPEC-011 ramped to 100%; feedback signals populating.
- [ ] W1, W2 landed. Migration in staging.

### Phase 1 — Aggregator behind flag (P0)
- [ ] W3–W6, W8 landed behind flag.
- [ ] Aggregation task enabled in shadow (writes
      `nutrition_learned_preferences` rows but planner still uses
      legacy path).
- [ ] Review 20 internal profiles' aggregated outputs: do chips
      align with what the team would say about themselves?

### Phase 2 — Planner integration (P0/P1)
- [ ] W9 planner prompt switched under the flag.
- [ ] Flag on for internal profiles; compare plan quality against
      flag-off (same profile, same week, dual-planned).
- [ ] Acceptance gate: team-lead judgment that personalized plans
      are at least as good as legacy, meaningfully better in ≥3 of
      10 cases.

### Phase 3 — UI + ramp (P1)
- [ ] W10 UI panel shipped; copy reviewed.
- [ ] 10% → 50% → 100% over three weeks.
- [ ] Metrics watched:
      - Override write rate per user (UI engagement signal).
      - `forget` action rate (system was wrong signal).
      - Plan regeneration rate (satisfaction proxy).

### Phase 4 — Cleanup (P1/P2)
- [ ] W7 NL summary cache; W11 clinician path; W12 observability;
      W13 rebuild tool.
- [ ] Flag default on. Remove legacy summarizer path in a later
      PR.

### Rollback
- Flag off → planner reverts to name-list; aggregator continues
  writing preference rows harmlessly.
- Overrides persisted; re-enable flag to resume surfaces.

---

## 6. Verification

### 6.1 Unit tests

- `test_decay_half_life.py` — 35-day-old score with no new feedback
  has half the pseudo-count weight.
- `test_note_vs_derived_weighting.py` — a `note` signal of strength
  0.5 outweighs a `derived` signal of 0.5 for the same key.
- `test_min_evidence_gate.py` — a single strong note does not
  appear in the digest when `N_MIN=3`; appears after three
  confirming events.
- `test_override_merge.py` — `pin_positive` wins over lower
  learned score; `exclude` removes the key from the digest; a
  subsequent `forget` wipes both learned and override state.
- `test_idempotent_apply.py` — applying the same
  `FeedbackSignals` twice updates the preferences once (matched by
  `last_extraction_id`).

### 6.2 Integration tests

- `test_feedback_to_digest.py` — 10 synthetic feedbacks landed →
  aggregator runs → digest contains the expected top signals.
- `test_digest_cold_start.py` — user with zero feedback returns
  empty digest and a politely empty `summary_nl`.
- `test_override_via_api.py` — PUT/DELETE endpoints round-trip
  correctly; audit log rows present.
- `test_rebuild.py` — delete preferences row, POST /rebuild,
  aggregator rebuilds identical state from feedback-signals history.

### 6.3 Property tests

- Monotonicity: strongly positive feedback on a key never
  decreases its strength.
- Bounded: `Score.mean ∈ [-1, +1]` and `Score.confidence ∈ [0, 1]`
  always.
- Determinism: replaying the same signal stream twice produces
  byte-equal preferences.

### 6.4 Reviewer audit (Phase 1)

- 20 internal profiles audited by team lead + a user. Acceptance:
  ≥15/20 profiles show chips that feel correct; no profiles show
  chips that feel wrong (vs. absent or incomplete).

### 6.5 Observability

- All §4.11 counters emit in staging.
- Cold-start dashboard shows the fraction of active users with
  fewer than `N_MIN` feedback events.

### 6.6 Copy review

- Chip labels, provenance strings, and empty-state copy reviewed.

### 6.7 Cutover criteria (flag-on)

- All P0/P1 tests green.
- Phase 2 acceptance gate met.
- Phase 3 metrics stable.
- Clinical reviewer + team lead sign-off.

---

## 7. Open Questions

- **`N_MIN` and decay rate calibration.** Defaults (`N_MIN=3`,
  `lambda=0.02/day`) are educated guesses. Phase 3 metrics will
  tell us whether to loosen or tighten.
- **NL summary freshness.** A weekly refresh might feel stale for
  very active users. We can switch to event-triggered refresh on
  digest-item changes; v1 is conservative to keep LLM cost bounded.
- **Clinician visibility of user overrides.** In v1 the clinician
  can see but not unconditionally override a user's own overrides
  — a user who has pinned "avoid cilantro" is respected. Revisit
  if clinical workflows require stronger authority.
- **Chip actions in UI — "Not really — forget this".** This
  resolves to a `forget` which allows re-learning. Some users will
  want "never learn this dimension again". That is an
  `exclude` action; we expose both in the UI under a progressive
  disclosure ("actually, turn this off permanently").
