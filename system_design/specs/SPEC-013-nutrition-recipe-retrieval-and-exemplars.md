# SPEC-013: Recipe retrieval and few-shot exemplar injection

| Field       | Value                                                    |
|-------------|----------------------------------------------------------|
| **Status**  | Proposed                                                 |
| **Author**  | Nutrition & Meal Planning team                           |
| **Created** | 2026-04-17                                               |
| **Priority**| P1 (capstone for ADR-004; unblocks preference-grounded planning and cook-mode UX) |
| **Scope**   | New module `backend/agents/nutrition_meal_planning_team/preferences/retrieval/`, embedding index, planner prompt wiring, background indexing task |
| **Depends on** | SPEC-011 (feedback signals), SPEC-012 (learned preferences digest), SPEC-005 (canonical ingredients), SPEC-009 (recipe features) |
| **Implements** | ADR-004 §5 (retrieval) |

---

## 1. Problem Statement

SPEC-012 gives the planner a structured digest ("leans toward
one-pan, chicken, umami"). That is a large jump in prompt quality
from the legacy name list. It is still abstract. Users recognize
their preferences in concrete recipes: "Yes, like the shakshuka we
had last month, not like that raw-tofu poke."

This spec ships the retrieval layer: embed every past
recommendation, and on each plan generation pull the top-N similar
past hits and the bottom-M recent misses into the meal-planning
prompt as few-shot exemplars. Combined with SPEC-012's digest and
SPEC-007's constraint block, the planner receives (abstract
features) + (concrete exemplars) + (hard constraints) — three
complementary grounding layers instead of one noisy list.

It is also where cold-start exploration lives: an ε-exploration
rule that keeps the plan from collapsing onto a narrow repeat menu.

---

## 2. Current State

### 2.1 After SPEC-012

- Planner receives a structured digest of learned dimensions.
- Past hits/misses are named in the digest but not retrieved with
  any similarity function — the planner is expected to pattern-
  match on names alone.

### 2.2 Gaps

1. No embedding index over past recipes.
2. No similarity retrieval on plan generation.
3. No exemplar injection surface in the planner prompt.
4. No cold-start exploration — purely exploitative generation
   converges on a narrow repeat menu.
5. No way to filter retrieved exemplars against the current
   profile's restrictions, so a formerly-loved meal that is now
   illegal (new allergy) could resurface.

---

## 3. Goals and Non-Goals

### 3.1 Goals

- Index every `MealRecommendation` with a stable embedding on
  write. Reindex the history on embedding-model upgrade.
- Expose `retrieve_exemplars(client_id, target_slot) -> Exemplars`
  that returns top-N past hits and bottom-M recent misses, filtered
  against the current profile's resolved restrictions
  (SPEC-006 / SPEC-007).
- Extend the meal-planner prompt with a short exemplars block
  (structured, numeric bounded).
- Add a deterministic ε-exploration rule: a small fraction of
  retrieval calls inject a deliberately dissimilar exemplar to
  preserve variety.
- Keep index reads O(1) per slot on the plan's critical path.
- Never retrieve a recipe whose restriction set is incompatible
  with the user's current profile (policy drift safety).

### 3.2 Non-goals

- **No new embedding model choice.** We pin on whatever the
  platform already exposes (coordinate with the AI Systems team's
  existing shared embedding provider); v1 choice is documented
  but swapping is a `RETRIEVAL_VERSION` bump.
- **No cross-user retrieval.** Collaborative filtering is a
  future ADR.
- **No retrieval for substitution.** ADR-005's substitution
  endpoint will use this module via the same `retrieve_exemplars`
  call shaped around a `SwapConstraint`; that wiring is ADR-005's
  job, not this spec's.
- **No UI in v1.** SPEC-012 owns the preference panel; this spec
  is backend-only.

---

## 4. Detailed Design

### 4.1 Module layout

```
backend/agents/nutrition_meal_planning_team/preferences/retrieval/
├── __init__.py            # retrieve_exemplars, index_recipe, RETRIEVAL_VERSION
├── version.py             # RETRIEVAL_VERSION = "1.0.0"
├── types.py               # Exemplars, Exemplar, EmbeddingVector
├── embed.py               # thin wrapper over the platform embedding provider
├── index.py               # pgvector-based index over recipe embeddings
├── retrieve.py            # top-K retrieval + restriction filter + exploration
├── reindex.py             # background reindex task
├── errors.py
└── tests/
```

### 4.2 Embedding input

For every `MealRecommendation` recorded (SPEC-007 recording step),
compute:

- A canonical text payload:
  ```
  name: {display_name}
  cuisine: {cuisine_tag}
  format: {format_tag}
  protein: {protein_tag}
  flavors: {flavor_tags}
  textures: {texture_tags}
  canonical_ingredients: {sorted canonical_ids}
  cooking_method: {cooking_method}
  ```
- The canonicalization is deterministic; two recipes with the same
  fields produce the same input string.
- The `canonical_ingredients` list is the SPEC-007-parsed set
  (`parsed_ingredients_present=true` rows only). Recipes where
  parsing produced unresolved ingredients are still indexed but
  their embedding is flagged `low_quality=true`.

### 4.3 Postgres index (pgvector)

Migration `009_recipe_embeddings.sql`:

```sql
-- Requires pgvector extension.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS nutrition_recipe_embeddings (
    recommendation_id  TEXT PRIMARY KEY
        REFERENCES nutrition_recommendations(recommendation_id) ON DELETE CASCADE,
    client_id          TEXT NOT NULL,
    embedding          vector(1024) NOT NULL,     -- dimension pinned at RETRIEVAL_VERSION major
    embedding_model    TEXT NOT NULL,
    canonical_text     TEXT NOT NULL,             -- the input used to embed; debug + diff
    low_quality        BOOLEAN NOT NULL DEFAULT FALSE,
    indexed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    retrieval_version  TEXT NOT NULL
);
CREATE INDEX ON nutrition_recipe_embeddings USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX ON nutrition_recipe_embeddings (client_id, indexed_at DESC);
CREATE INDEX ON nutrition_recipe_embeddings (retrieval_version);
```

`vector(1024)` is a placeholder dimension; set at spec-freeze time
to match the chosen embedding model. Model choice documented in
`retrieval/README.md` and pinned via `RETRIEVAL_VERSION`.

### 4.4 Indexing pipeline

Synchronous path (critical path): indexing is **not** synchronous
in the plan generation path. Instead:

1. On SPEC-007 `_record_suggestions`, emit an event
   `recipe.indexable` with the recommendation_id and canonical
   text.
2. A background task in the nutrition team consumes the event and
   runs `embed.embed_one(text)` + INSERT into
   `nutrition_recipe_embeddings`.
3. First-plan users' initial recommendations may not be indexed
   yet during their first retrieval call — this is fine, retrieval
   falls back gracefully.

`reindex.py` CLI rebuilds the entire index for a `client_id` or
globally when `RETRIEVAL_VERSION` major bumps (embedding-model
swap). Global reindex runs as a scheduled task; global reindex is
admin-gated.

### 4.5 Retrieval (`retrieve.py`)

```python
@dataclass(frozen=True)
class Exemplar:
    recommendation_id: str
    display_name: str
    kind: Literal["hit", "miss", "exploration"]
    rating_at_retrieval: Optional[int]
    similarity: float
    canonical_ingredient_ids: tuple[str, ...]
    ingredients_preview: tuple[str, ...]    # for the planner prompt
    age_days: int

@dataclass(frozen=True)
class Exemplars:
    hits: list[Exemplar]                    # top-N similar past hits
    misses: list[Exemplar]                  # bottom-M recent misses
    exploration: list[Exemplar]             # 0 or 1 dissimilar hit (see §4.7)
    filter_summary: dict[str, int]          # why recipes were filtered out

def retrieve_exemplars(
    client_id: str,
    target_slot: TargetSlot,     # meal_type, suggested_date, period
    profile: ClientProfile,      # for restriction filter
    digest: PreferenceDigest,    # from SPEC-012; shapes the query
    n_hits: int = 5,
    m_misses: int = 3,
    epsilon: float = 0.1,
) -> Exemplars: ...
```

Retrieval steps:

1. **Build query vector.** Use the digest's top positive signals
   as a synthetic "ideal meal" text (one-pan, chicken, umami,
   etc.) and embed it via `embed.embed_one`. This is the query
   vector. Alternative: average-of-top-hits vector — documented,
   chosen at implementation based on quality tests.
2. **Candidate pool.** Fetch the top N+M+50 most similar past
   recommendations for this client (by cosine), not filtered yet.
   50 buffer covers filter attrition.
3. **Restriction filter.** For each candidate, run
   `guardrail.check_recommendation` (SPEC-007) against the
   **current** profile. Any hard reject is excluded.
4. **Rating filter.** A candidate qualifies as a `hit` if its
   feedback indicates positive (rating ≥ 4 or `wma=true`). A
   `miss` if recent (≤ 60 days) and feedback negative.
5. **Deduplication.** Never return two exemplars with identical
   `canonical_ingredient_ids`; take the more recent one.
6. **Selection.** Top N hits (after filter) and bottom M misses
   (most recent first).
7. **Exploration.** With probability `epsilon`, include one
   `exploration` exemplar: a past hit with the **lowest** cosine
   similarity that still passes filters. Exploration keeps variety
   without violating preferences.
8. **Cold start.** Fewer than `N_COLD` (default 10) past
   recommendations: return empty `hits`/`misses` and signal
   cold-start to the caller.

Retrieval is deterministic when `epsilon=0`. When `epsilon>0`, the
exploration step uses a seeded RNG derived from `client_id +
date(today)` so the same day's plan is reproducible.

### 4.6 Planner prompt integration

The meal planner prompt (after SPEC-010) receives a short exemplars
block:

```
Past meals this user liked (for style grounding):
  - Chicken shawarma bowl (rating 5, made 12 days ago)
      ingredients: chicken_thigh_raw, garlic, lemon, tahini, cucumber, tomato, parsley
  - Sheet-pan gnocchi (rating 5, made 34 days ago)
      ingredients: gnocchi, cherry_tomato, olive_oil, mozzarella, basil
  - Shakshuka (rating 4, wma, made 3 days ago)
      ingredients: tomato_canned, egg, bell_pepper, onion, paprika, cumin
  Past miss (avoid similar style):
  - Raw-tofu poke (rating 2, made 8 days ago)
      ingredients: silken_tofu_raw, soy_sauce, sesame_oil, seaweed, rice

Prefer recipes that share ingredients, format, and flavor profile with
the hits. Avoid patterns similar to the miss.
```

The planner combines this with SPEC-012's digest and SPEC-007's
constraint block. Exemplar ingredient lists are trimmed to top-7
by mass to keep the prompt compact.

### 4.7 Exploration policy

- `epsilon = 0.1` means 1 in 10 retrievals surfaces a deliberately
  dissimilar past hit.
- The surfaced exploration exemplar is labeled as such in the
  prompt: *"Exploration pick (something they liked that's unlike
  their typical plan):"* so the planner treats it as a variety
  signal rather than a pattern to match.
- User-visible: SPEC-012's "Adjusted" transparency applies — we
  note "added for variety" in the recipe rationale when the plan
  borrows from an exploration exemplar.

### 4.8 Safety — never resurface illegal hits

- Restriction filter runs at retrieval time against the current
  profile (not the profile the recipe was recorded under). A user
  who develops a new allergy sees no past hits they can't safely
  eat.
- On `GUARDRAIL_VERSION` or `KB_VERSION` bump (SPEC-007 §4.10),
  the replay job already re-checks past recommendations. Retrieval
  reads the current guardrail result; nothing additional needed.
- Unit tests assert this invariant on multiple fixtures (§6).

### 4.9 Performance

- Candidate fetch: pgvector cosine query over ≤10k rows per user;
  target ≤ 30 ms.
- Restriction filter: SPEC-007 p99 ≤ 10 ms × 50 candidates =
  ≤ 500 ms in worst case; run concurrently across the candidate
  set.
- Query embedding: 1 LLM (embedding) call; target ≤ 200 ms.
- Total retrieval budget: ≤ 1 s added to the plan path, run in
  parallel with other planner-preparation work so the wall-clock
  impact is ≤ 200 ms effective.

### 4.10 Privacy

- Embeddings are derived artifacts of recipes the user ate or was
  shown; deleted on account deletion via cascade.
- Embeddings do not leave the nutrition team's Postgres schema;
  no external retrieval services in v1.
- Canonical text is stored alongside the embedding for
  reconstruction and audit; redacted with respect to user PII by
  construction (the canonical text is recipe content, not profile
  content).

### 4.11 Versioning

`RETRIEVAL_VERSION = "MAJOR.MINOR.PATCH"`:

- **MAJOR** — embedding model change, dimension change, canonical
  text shape change. Global reindex required.
- **MINOR** — retrieval algorithm tweaks (k, epsilon defaults,
  scoring).
- **PATCH** — bug fixes.

### 4.12 Priority-grouped work items

| # | Item | Priority |
|---|------|----------|
| W1 | Module scaffolding, version, types | P0 |
| W2 | Migration `009_recipe_embeddings.sql` (pgvector) + schema registration | P0 |
| W3 | `embed.py` wrapper over platform embedding provider | P0 |
| W4 | Canonical text builder from a `MealRecommendation` | P0 |
| W5 | Background indexing task consuming `recipe.indexable` events | P0 |
| W6 | `retrieve.py` top-K + restriction filter + deduplication | P0 |
| W7 | ε-exploration rule with seeded RNG tests | P0 |
| W8 | Planner prompt integration (exemplars block) | P1 |
| W9 | Cold-start path: return empty exemplars + signal | P0 |
| W10 | `reindex.py` CLI + scheduled global reindex on MAJOR bump | P1 |
| W11 | Observability counters + filter summary in Exemplars response | P1 |
| W12 | Benchmarks: retrieval p99 ≤ 1 s end-to-end | P2 |
| W13 | Exploration transparency: rationale-level tag on exploration-sourced recipes | P2 |

---

## 5. Rollout Plan

Flag `NUTRITION_RECIPE_RETRIEVAL` (off → planner receives only
digest + constraints, on → exemplars block added).

### Phase 0 — Foundation (P0)
- [ ] SPEC-012 at 100% ramp.
- [ ] pgvector extension enabled in staging.
- [ ] W1–W4 landed.

### Phase 1 — Indexing + shadow (P0)
- [ ] W5 background task live; start indexing new recommendations.
- [ ] Backfill existing recommendations with `reindex.py` (rate-
      limited).
- [ ] Shadow retrieval: for flag-off users, call
      `retrieve_exemplars` and log results without injecting into
      the prompt. Review quality on 20 internal profiles.

### Phase 2 — Planner integration (P0/P1)
- [ ] W6, W7, W8, W9 landed.
- [ ] Flag on internal. Compare plans with and without exemplars
      (dual generation on the same input).
- [ ] Acceptance gate: reviewer judges the exemplar-augmented
      plans more recognizable ("feels like something I'd actually
      eat") in ≥6 of 10 cases.

### Phase 3 — Ramp (P1)
- [ ] 10% → 50% → 100% over two weeks.
- [ ] Monitor retrieval latency, filter attrition (recipes blocked
      by current-profile restriction), exploration rate.
- [ ] Safety check: zero instances of a past hit being retrieved
      that fails the current guardrail. Fix immediately if any
      observed.

### Phase 4 — Cleanup (P1/P2)
- [ ] W10–W13 landed.
- [ ] Flag default on; removal scheduled.

### Rollback
- Flag off → planner receives digest only; exemplars ignored.
  Index continues populating harmlessly.
- Migration is additive; rollback not needed.

---

## 6. Verification

### 6.1 Unit tests

- `test_canonical_text_stable.py` — same recipe fields → same
  canonical text → same embedding input.
- `test_restriction_filter.py` — candidate with a newly-added
  allergen is excluded even when it was a past hit.
- `test_deduplication.py` — two recipes with identical canonical
  ingredient sets collapse to the most recent.
- `test_cold_start.py` — client with < `N_COLD` past
  recommendations returns empty exemplars and the cold-start
  signal.
- `test_exploration_seeded.py` — same `client_id + date(today)`
  → identical exploration pick across runs; different days →
  different picks.

### 6.2 Integration tests

- `test_indexing_event_to_row.py` — recording a new recommendation
  emits `recipe.indexable`; background task inserts a row within
  30 s; row has correct canonical text and embedding dimension.
- `test_retrieve_with_guardrail_violation.py` — past hit contains
  a now-forbidden ingredient; retrieve_exemplars excludes it;
  `filter_summary` reflects the exclusion.
- `test_retrieval_latency.py` — end-to-end retrieval for a user
  with 500 indexed recommendations completes within budget.

### 6.3 Safety tests

- `test_no_illegal_resurfacing.py` — matrix of (profile change,
  past hit): for each, assert retrieve never returns the illegal
  one. Runs on dozens of combinations.
- `test_guardrail_drift_policy_bump.py` — simulate a
  `GUARDRAIL_VERSION` bump mid-flight; retrieval reads the current
  guardrail.

### 6.4 Reviewer audits (Phase 1, Phase 2)

- Phase 1 shadow retrieval: 20 internal profiles, reviewer (team
  lead + one non-team user) rates exemplar quality on a 1–5
  scale. Median ≥ 4.
- Phase 2 planner integration: 10 paired plans with and without
  exemplars. Acceptance: reviewer prefers the exemplar-augmented
  plan in ≥6 cases.

### 6.5 Property tests

- Monotonicity: a user with more and more positive feedback on a
  dimension will retrieve more hits that share that dimension.
- Filter correctness: no `kind=hit` exemplar violates the current
  profile's restrictions.

### 6.6 Observability

- `retrieval.candidates_fetched`, `retrieval.filter_attrition`,
  `retrieval.exploration_rate`, `retrieval.latency_ms` histograms.
- Alerting: exploration rate deviating > 2× from `epsilon` in a
  rolling hour → investigate (seed bug or policy regression).

### 6.7 Cutover criteria

- All P0/P1 tests green.
- Phase 2 acceptance gate met.
- Zero safety test failures; zero observed illegal-resurfacing
  incidents during ramp.
- Clinical reviewer + team lead sign-off.

---

## 7. Open Questions

- **Query-vector strategy.** "Synthetic ideal meal from digest"
  vs. "average of top hits" vs. "concat of both". v1 picks one
  based on shadow-phase quality tests; the alternative is a minor
  bump away.
- **Dimension pinning.** Embedding dimension is model-dependent.
  Pinning at 1024 is a placeholder; chosen per actual model at
  spec-freeze time and carved into `RETRIEVAL_VERSION` major.
- **pgvector vs. external vector store.** v1 keeps data local
  (privacy win; simpler deploy). At scale (>100k recipes per
  user, unrealistic) we could revisit; no plan to do so.
- **User-visible exploration label.** §4.7 notes "added for
  variety" on the rationale. We may want a user-facing toggle
  ("surprise me more / less") — v1.1 feature; not in scope here.
- **Substitution via retrieval.** ADR-005 will add a
  `SwapConstraint`-shaped retrieval call. The module exposes the
  internals (`retrieve.py` takes a `query_builder` parameter) so
  ADR-005 can reuse without forking.
