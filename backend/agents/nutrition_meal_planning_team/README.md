# Nutrition & Meal Planning Team

Personal nutritionist and meal-planning agents that learn from feedback. The team provides dietary planning (nutrient targets, balance guidelines) and recipe/meal suggestions tailored to household size, dietary needs, allergies, lifestyle (cooking time, lunch context), and preferences. Recommendations improve over time using ratings and “would make again” feedback.

## Endpoints

All routes are under the unified API prefix **`/api/nutrition-meal-planning`**.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Team health check |
| GET | `/profile/{client_id}` | Get client profile (404 if not found) |
| PUT | `/profile/{client_id}` | Create or update profile; body: partial profile (intake agent validates/completes) |
| POST | `/plan/nutrition` | Get nutrition plan; body: `client_id`, optional `date_range_start`/`date_range_end` |
| POST | `/plan/meals` | Get meal plan; body: `client_id`, `period_days`, `meal_types`; returns suggestions with `recommendation_id` for feedback |
| POST | `/plan/meals/async` | Start async meal plan job; returns `job_id`; poll `GET /jobs/{job_id}` for result |
| GET | `/jobs/{job_id}` | Job status and result (for async meal plan) |
| POST | `/feedback` | Submit feedback; body: `client_id`, `recommendation_id`, `rating`, `would_make_again`, `notes` |
| GET | `/history/meals?client_id=...` | Past recommendations and feedback for the client |

## Client profile fields

Stored per client and used by all agents:

- **household**: `number_of_people`, `description` (e.g. solo, couple, family of 4), `ages_if_relevant`, `members` (optional list per person: `name`, `age_or_role`, `dietary_needs`, `allergies`, `notes`)
- **dietary_needs**: e.g. vegetarian, vegan, keto, low-sodium, diabetic-friendly
- **allergies_and_intolerances**: e.g. nuts, shellfish, gluten
- **lifestyle**: `max_cooking_time_minutes`, `lunch_context` (`"office"` or `"remote"`), `equipment_constraints`, `other_constraints`
- **preferences**: `cuisines_liked`, `cuisines_disliked`, `ingredients_disliked`, `preferences_free_text`
- **goals**: `goal_type` (e.g. maintain, lose_weight), `notes`

## Learning-from-feedback flow

1. **Recommend**: `POST /plan/meals` returns meal suggestions; each suggestion has a `recommendation_id`. The API records each recommendation in the meal/feedback store.
2. **Feedback**: Client submits `POST /feedback` with `recommendation_id`, `rating` (e.g. 1–5), `would_make_again`, and optional `notes`.
3. **Learning**: On the next `POST /plan/meals`, the meal planning agent receives a summary of “past hits” (high rating / would make again) and “past misses” (low rating / would not make again) and prefers similar meals to hits and avoids similar to misses.

Data is persisted in Postgres (tables `nutrition_profiles`, `nutrition_conversations`, `nutrition_plans`, `nutrition_recommendations`). Schema is registered from the team's FastAPI lifespan via `shared_postgres.register_team_schemas`; connection is configured through the usual `POSTGRES_*` env vars. No local/filesystem cache is used.

## Agents

- **Intake / profile agent**: Validates and completes client profile from partial updates (LLM-backed). If the LLM is unavailable, updates are merged structurally so `PUT /profile/{id}` still persists the payload.
- **Nutritionist agent**: Produces a **nutrition plan** (daily targets, balance guidelines, foods to emphasize/avoid) from profile; no recipes.
- **Meal planning agent**: Produces recipe/meal suggestions from profile, nutrition plan, and meal history (with feedback summary) so recommendations improve over time.

## Dependencies

Uses the Personal Assistant team’s LLM client (`personal_assistant_team.shared.llm`) for completion and JSON extraction. Requires `personal_assistant_team` to be available when the API runs.

## Durable execution (Temporal)

All three async job endpoints — `POST /plan/nutrition`, `POST /plan/nutrition/{client_id}/regenerate`, and `POST /plan/meals` — run as Temporal workflows/activities when `TEMPORAL_ADDRESS` is set, and fall back to daemon threads otherwise. The wiring follows the shared `shared_temporal` pattern (annotation-based):

- `temporal/workflows.py` defines one `@workflow.defn` class and one `@activity.defn` function per job kind (`NutritionPlanWorkflow`/`run_nutrition_plan_activity`, `NutritionRegenerateWorkflow`/`run_nutrition_regenerate_activity`, `NutritionMealPlanWorkflow`/`run_meal_plan_activity`). Each activity delegates to the neutral `pipeline.py` core (shared with the thread path) so job-store bookkeeping and cancel semantics live in one place and the durable worker never imports the FastAPI app.
- `temporal/__init__.py` exports `WORKFLOWS`/`ACTIVITIES`/`TASK_QUEUE` (queue `nutrition-meal-planning`); the worker boots via `temporal/worker.py::start_nutrition_temporal_worker_thread` (docker-compose `TEAM_TEMPORAL_WORKER_*` hook) with the API lifespan as a standalone backstop.
- Regardless of mode, job status is written to the durable `JobServiceClient` store and polled at `GET /jobs/{job_id}`, so a completed run survives a worker/process restart.

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
