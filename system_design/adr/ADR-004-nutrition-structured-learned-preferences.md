# ADR-004 — Structured learned preferences: extract, store, and retrieve per-client taste signals

- **Status**: Proposed
- **Date**: 2026-04-17
- **Owner**: Nutrition & Meal Planning team
- **Related**: ADR-002 (canonical ingredients), ADR-003 (nutrient rollup), `backend/agents/nutrition_meal_planning_team/agents/meal_planning_agent/`

## Context

The learning-from-feedback loop is the team's product thesis (README
section "Learning-from-feedback flow"). The implementation is thinner
than the pitch:

- `_summarize_history` (`agents/meal_planning_agent/agent.py`) produces
  `"Past hits (they liked these): chicken tikka, shakshuka, ..."` and
  `"Past misses (avoid similar): tofu stir-fry, ..."` — a
  comma-separated list of **meal names, up to 15 each**.
- Free-text `notes` on `FeedbackRecord` ("too spicy", "loved the
  one-pan idea, but too much prep") are discarded by the summarizer.
- The binary `would_make_again` and the 1–5 rating are the only
  structured signal that survives.
- There is no retrieval or embedding step; the LLM is expected to
  generalize from raw names.

Three consequences:

1. **Slow convergence.** A user needs 10–15 rounds of feedback before
   the system reliably matches their preferences, because meal names
   carry almost no generalization signal ("chicken tikka" doesn't tell
   the model whether they liked the chicken, the sauce, the spice, or
   the one-pan format).
2. **Wasted signal.** The richest data users give us — free-text notes
   — is thrown away.
3. **No trust surface.** Users cannot see what the system has learned
   about them. In consumer health, *visible* learning is the single
   biggest driver of stickiness; invisible learning is indistinguishable
   from no learning.

We need structured preference features that the planner can condition
on and the user can inspect and correct.

## Decision

Introduce a `LearnedPreferences` record per client, updated after every
feedback submission, stored in Postgres, and fed into the
meal-planning prompt and a retrieval step.

### 1. Preference dimensions (v1 taxonomy)

Fixed, closed set — extensible in minor versions only:

- `ingredient_affinity: dict[canonical_id, score]` — signed score per
  canonical ingredient (uses ADR-002's ids).
- `cuisine_affinity: dict[cuisine_tag, score]` — `italian`,
  `south_indian`, `levantine`, etc.
- `flavor_affinity: dict[flavor_tag, score]` — `spicy`, `umami`,
  `sweet`, `sour`, `bitter`, `fermented`, `smoky`.
- `texture_affinity: dict[texture_tag, score]` — `crispy`, `creamy`,
  `chewy`, `crunchy`, `soft`.
- `format_affinity: dict[format_tag, score]` — `one_pan`, `sheet_pan`,
  `no_cook`, `meal_prep`, `handheld`, `bowl`, `soup_stew`.
- `protein_affinity: dict[protein_tag, score]` — `chicken`, `beef`,
  `pork`, `fish`, `shellfish`, `tofu`, `tempeh`, `seitan`, `legume`.
- `effort_tolerance: {weekday, weekend}` — learned cooking-time band,
  separate from the profile's hard `max_cooking_time_minutes` (which
  is an absolute limit).
- `portion_skew: float` — learned ratio of served vs. user's preferred
  portion size (from notes like "too much", "not filling").
- `novelty_preference: float` — how often this user rewards unfamiliar
  meals vs. repeats of hits.

All `score` fields are signed floats, not raw counts (see §3).

### 2. Feedback extraction

Add `nutrition_meal_planning_team/preferences/extractor.py`:

- Input: `(MealRecommendation, FeedbackRecord)` and the current
  `LearnedPreferences`.
- Small LLM call with a strict structured-output schema (via
  `llm_service` structured-output contract from PR #184) returning a
  `FeedbackSignals` object: dimensions present in this feedback with
  signed strengths in `[-1, 1]`.
- Input to the LLM: the meal's canonical ingredients + cuisine/format
  tags (derived, not LLM-inferred) + the user's free-text note + the
  rating + `would_make_again`.
- Parsed signals are deterministic from that point on.

Important: the LLM extracts *what the note says*, not *what it
predicts*. It is a parser, not a recommender.

### 3. Update rule

Scores use a **Beta / Bayesian-smoothed EWMA**, not raw counts:

- Each dimension tracks `(alpha, beta)` pseudo-counts plus a decay
  factor. Posterior mean maps to the signed score.
- Recent feedback weighs more (decay λ ≈ 0.02 per day), so tastes are
  allowed to drift.
- Explicit rating strength modulates update magnitude (5★ + "loved"
  note → larger update than 4★ silent).
- Free-text "loved" / "hated" signals from extractor contribute
  independently of the numeric rating.
- Minimum-evidence gate: dimensions with fewer than `N_MIN` (default 3)
  pieces of supporting feedback are not surfaced in the planner prompt
  or the UI. We do not want to claim "you hate cilantro" after one
  meal.

### 4. Planner integration

`meal_planning_agent.run` is changed to receive a
`PreferenceDigest` — a trimmed view of `LearnedPreferences`:

- Top-K positive and top-K negative signals across all dimensions
  (default K=6), each with a confidence band.
- A short natural-language summary (generated once per digest update,
  cached) — *"Leans toward one-pan meals, spicy + umami flavors,
  chicken and legumes. Avoids very long prep on weekdays. Meh on raw
  tofu but fine on crispy tofu."* — for prompt economy.
- The meal-names list is reduced from 15 to the top 5 highest-rated
  hits, primarily for retrieval (§5), not for the LLM to pattern-match.

The prompt now contains features, not names. Generalization improves
by a large margin because the LLM can reason over dimensions rather
than guess patterns from meal titles.

### 5. Retrieval

Add `preferences/retrieval.py`:

- Embed every past recommendation on write (using an already-available
  embedding model; ingredient + cuisine + format + name).
- On plan generation, retrieve top-N similar past hits (N=5) and
  bottom-M recent misses (M=3) as few-shot exemplars in the prompt.
- Retrieval is filtered by the current profile constraints (allergies,
  dietary) before being injected, to avoid surfacing now-illegal
  exemplars.

Exemplars + digest together give the planner both concrete patterns
and abstract features.

### 6. User-visible preference panel

New endpoints:

- `GET /preferences/{client_id}` — returns `LearnedPreferences` with
  confidence bands, plus the `PreferenceDigest` shown to the planner.
- `PUT /preferences/{client_id}/override` — user can pin ("I do like
  mushrooms, stop avoiding them") or exclude dimensions from the
  prompt. Overrides live alongside learned scores and always win.
- `DELETE /preferences/{client_id}/signal/{dim}/{key}` — forget a
  single learned point. Required for the UX, and sometimes for
  compliance (user correcting an embarrassing wrong inference).

UI implication (tracked separately): editable chips on the profile
page, e.g. `Loves one-pan ✓` `Avoids cilantro ✓ (edit)`. This is
where trust is earned.

### 7. Cold-start

- For users with no feedback, `LearnedPreferences` is empty; planner
  falls back to today's profile-only behavior.
- Optional onboarding quiz (post-intake): 10 images / descriptions, 1-
  tap like/dislike, seeds the dimensions with low-confidence priors.
  Cuts cold-start rounds roughly in half based on analogous product
  data; treat as v1.1 if onboarding UX slips.

### 8. Privacy and data lifecycle

- `LearnedPreferences` is personal data. Stored in
  `nutrition_preferences` table; deleted on account deletion alongside
  profile.
- Extractor LLM calls do not include `client_id` or free-text notes in
  logs above DEBUG. Structured `FeedbackSignals` are retained; raw
  notes are retained only where the user account retains them.
- Overrides and deletions are honored before the next planner call
  (no async propagation window for privacy-sensitive operations).

## Consequences

### Positive

- Personalization converges in ~3–5 rounds instead of 10–15; the
  signal per feedback event is much higher because notes are no
  longer discarded.
- Free-text feedback becomes a first-class data product, not a
  write-only field.
- Visible, editable learned preferences — the single biggest driver of
  trust in consumer food apps. Users can correct us; the system feels
  cooperative rather than opaque.
- Retrieval unlocks few-shot grounding, which is more robust to prompt
  and model changes than leaning on raw training priors.
- Clean separation: *extraction* is LLM, *update* is deterministic,
  *retrieval* is deterministic, *narration* is LLM. Each layer is
  testable on its own.

### Negative / costs

- **Extra LLM call per feedback.** One structured-output call per
  feedback record. Small, cheap, and asynchronous, but non-zero.
  Mitigation: run in the feedback-endpoint background task; if the
  extractor fails, store the raw feedback and retry — user-facing
  response does not wait.
- **Privacy surface grows.** Storing learned taste data is richer than
  storing raw ratings. Warrants a short privacy review and a visible
  "what we've learned" page (built as part of §6 — solves product
  trust and compliance simultaneously).
- **Taxonomy is opinionated.** The v1 dimensions are a guess at what
  matters. Some dimensions will carry no signal; others will be
  missing. Mitigation: log `dim_hit_rate` and revise the taxonomy on a
  cadence; keep it closed in prod to avoid score-schema churn.
- **Embeddings add a moving part.** A new index, a new model version to
  track, and a reindex cost on model swap. Keep the embedding choice
  conservative and swap-aware (version the vectors).
- **Can over-specialize.** Pure exploitation converges on a narrow
  menu. `novelty_preference` plus an ε-exploration term in the
  retrieval call (default ε=0.1) preserves variety.

### Neutral / follow-ups

- The cook-mode feedback surface (ADR-005) multiplies the volume of
  signal by ~10× (every cooked meal emits structured signals, not
  just ones the user bothers to rate). This ADR is designed to
  absorb that volume with no schema change.
- Adherence tracking gives us a third axis (did they actually eat it)
  that the update rule can consume in v2.
- A future ADR can consider cross-user collaborative signals (matrix
  factorization over the same dimensions); out of scope for v1 and
  has its own privacy calculus.
