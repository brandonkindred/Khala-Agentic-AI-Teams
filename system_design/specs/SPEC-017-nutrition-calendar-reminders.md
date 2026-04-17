# SPEC-017: Calendar sync and prep-time-aware reminders

| Field       | Value                                                    |
|-------------|----------------------------------------------------------|
| **Status**  | Proposed                                                 |
| **Author**  | Nutrition & Meal Planning team (+ Integrations team coordination) |
| **Created** | 2026-04-17                                               |
| **Priority**| P2 within ADR-005 (last-mile launch; high perceived value, low risk) |
| **Scope**   | New module `backend/agents/nutrition_meal_planning_team/calendar_sync/`, Google Calendar integration via Integrations team, Postgres sync state, UI controls |
| **Depends on** | SPEC-002 (`timezone` on profile), SPEC-009 (`prep_time_minutes`, `cook_time_minutes`), SPEC-018 (cook-mode deep links, optional) |
| **Implements** | ADR-005 §4 (calendar + reminders) |

---

## 1. Problem Statement

Users plan a week of meals and then forget to start Tuesday's
shakshuka until 8:15 pm. The recipe needed 40 minutes of prep +
cook, and dinner slips to 9.

This spec syncs a plan's recipes to the user's Google Calendar as
events placed at the user's meal windows, with reminders scheduled
backwards from the mealtime by `prep_time + cook_time + buffer`.
Event descriptions deep-link into cook mode (SPEC-018) and include
the ingredient checklist and nutrient summary.

It is deliberately the *last* ADR-005 capability: low technical
risk (the Integrations team owns Google OAuth), high perceived
value, and it crystallizes the "weekly operating system" thesis
— the plan reaches the user at the moment they need it.

---

## 2. Current State

### 2.1 Today

- Plans carry optional `suggested_date`.
- Profile carries `timezone` (from SPEC-002).
- No calendar integration; users copy-paste into their own calendars
  or forget.

### 2.2 What exists on the platform

Per CLAUDE.md and the existing `backend/agents/integrations/`:
- Shared Google browser login and session infrastructure.
- The team already has the primitives for Google OAuth and
  Playwright-based fallbacks.

This spec reuses that infrastructure; it does not build new OAuth.

### 2.3 Gaps

1. No sync endpoint.
2. No event format or description standard.
3. No reminder math tied to recipe prep + cook times.
4. No idempotent delete/replace path for when a plan changes.
5. No profile field for user's preferred meal windows.

---

## 3. Goals and Non-Goals

### 3.1 Goals

- Ship `POST /plan/meals/{plan_id}/calendar/sync` that creates
  calendar events for each dated recipe, with prep-time-aware
  reminders firing before mealtime.
- Reuse the Integrations team's Google Calendar client; no new
  OAuth surface built in this spec.
- Idempotent: re-syncing the same plan updates existing events
  rather than creating duplicates. Dry-run mode available.
- Namespaced events: every event description begins with
  `[Khala Meals]` and carries a plan_id tag so users can filter
  and we can cleanly remove.
- Mealtime windows are per-profile; default sensible values; user-
  editable. Timezone respected per SPEC-002.
- DELETE endpoint removes all sync'd events for a plan.
- Blast-radius minimization: new calendar writes require explicit
  user opt-in per plan; a "dedicated calendar" option exists for
  users who want separation.

### 3.2 Non-goals

- **No Apple iCloud / Outlook / other providers in v1.** Google
  only. Others ride on the same event shape in follow-up specs.
- **No in-app calendar view.** We write to the user's Google
  Calendar; the UI shows "synced ✓" state and a link.
- **No shared-calendar multi-household sync.** One user's calendar
  per sync.
- **No grocery-run calendar events.** A future v1.1 idea; not in
  scope.
- **No recipe-level timing (step timers).** SPEC-018 cook-mode owns
  that. This spec is only about calendar events.

---

## 4. Detailed Design

### 4.1 Module layout

```
backend/agents/nutrition_meal_planning_team/calendar_sync/
├── __init__.py            # sync_plan_to_calendar, delete_plan_events, CAL_VERSION
├── version.py             # CAL_VERSION = "1.0.0"
├── types.py               # CalendarSyncState, EventDraft, SyncResult
├── builder.py             # plan → list[EventDraft]
├── reminder.py            # prep+cook time reminder calculation
├── sync.py                # idempotent upsert against Google Calendar
├── google_adapter.py      # thin wrapper over integrations team's client
├── errors.py
└── tests/
```

### 4.2 Profile additions

Additive on `ClientProfile.preferences`:

```python
class MealtimeWindow(BaseModel):
    meal_type: str                 # 'breakfast' | 'lunch' | 'dinner' | 'snack'
    hour: int                      # 0..23
    minute: int                    # 0..59
    day_override: Dict[int, tuple[int, int]] = {}
    # per-weekday overrides keyed by weekday number 0=Mon

class PreferencesInfo(BaseModel):
    # existing...
    mealtime_windows: list[MealtimeWindow] = []    # defaults seeded at profile creation
    calendar_buffer_minutes: int = 10              # extra time added after cook
    prefer_dedicated_calendar: bool = False
```

Defaults on new profiles: breakfast 07:30, lunch 12:30, dinner
19:00. Users edit in the UI (existing preferences screen gains a
"mealtimes" section).

### 4.3 Event format

```python
@dataclass(frozen=True)
class EventDraft:
    recipe_id: str
    plan_id: str
    title: str                     # "[Khala Meals] Shakshuka (dinner)"
    description: str               # see below
    start: datetime                # localized to profile timezone
    end: datetime
    reminders_minutes_before: tuple[int, ...]  # prep+cook+buffer + courtesy
    color: Optional[str] = None
    source_id: str                 # idempotency key
```

Description template:

```
[Khala Meals]
{recipe.display_name}
Meal: {meal_type} • Serves {portions}
Prep: {prep_time} min • Cook: {cook_time} min • Total: {total} min

Ingredients (for {portions} servings):
 - 400 g chicken thighs
 - 1 can tomato
 - ...

Nutrients per serving:
 {kcal} kcal • {protein_g} g P • {carbs_g} g C • {fat_g} g F

Open in cook mode: {deep_link}   (SPEC-018)

— Plan {plan_id}
```

The trailing `Plan {plan_id}` line is the idempotency anchor: sync
parses it to identify events this plan owns, enabling idempotent
upsert and clean delete.

### 4.4 Reminder math

For each dated recipe:

```
mealtime    = combine(suggested_date, profile.mealtime_windows[meal_type], profile.timezone)
cook_start  = mealtime - cook_time
prep_start  = cook_start - prep_time - buffer
event_start = prep_start
event_end   = mealtime + 30 min    # eating window
reminders   = [
    prep_time + cook_time + buffer,   # primary: "start cooking now"
    courtesy_reminder_minutes,        # user-configurable, default 60 (e.g. "dinner tonight")
]
```

If `prep_time` or `cook_time` is missing on the recipe, default to
conservative values (15 + 20 min) and flag the event
description with a note: *"(timing estimated)"*.

### 4.5 Sync API

| Method | Path | Purpose |
|--------|------|---------|
| `POST`   | `/plan/meals/{plan_id}/calendar/sync` | Create or update events. Body: `{dry_run?: bool, use_dedicated_calendar?: bool, only_meal_types?: list[str]}` |
| `DELETE` | `/plan/meals/{plan_id}/calendar/sync` | Remove all events for this plan |
| `GET`    | `/plan/meals/{plan_id}/calendar/sync` | Current sync state and event ids |
| `GET`    | `/calendar/connected/{client_id}` | Returns `{connected: bool, email?: str}` — thin status wrapper over integrations team's auth state |

Dry-run returns the event drafts the system would create without
writing anything; the UI uses this for a preview modal.

### 4.6 Idempotent upsert

Sync is keyed on `source_id` (recipe_id + plan_id). The sync task:

1. Query existing events in the target calendar where
   `description` matches the `Plan {plan_id}` anchor OR
   `source_id` extended property matches.
2. Build the set of desired events from the plan.
3. Diff:
   - Present in plan and calendar → update if any field changed.
   - Present in plan only → create.
   - Present in calendar only → delete.
4. Return a `SyncResult` summarizing created/updated/deleted counts
   and any errors.

The Google Calendar event carries an `extendedProperties.private`
entry `{"khala_plan_id": plan_id, "khala_recipe_id": recipe_id}`
for robust matching (description parsing as fallback).

### 4.7 Dedicated calendar mode

If `prefer_dedicated_calendar=true`:

1. Ensure a calendar named "Khala Meals" exists (create if not).
2. Write events there.
3. User can hide/show the calendar without affecting their primary
   view.

Default is primary calendar; the dedicated mode is a safety valve
for users nervous about event pollution.

### 4.8 Persistence

Migration `013_calendar_sync.sql`:

```sql
CREATE TABLE IF NOT EXISTS nutrition_calendar_sync (
    plan_id                     TEXT PRIMARY KEY,
    client_id                   TEXT NOT NULL,
    google_calendar_id          TEXT,
    events_json                 JSONB NOT NULL,   -- recipe_id -> google event id
    synced_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    cal_version                 TEXT NOT NULL,
    last_result_json            JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS nutrition_calendar_sync_audit (
    id            BIGSERIAL PRIMARY KEY,
    plan_id       TEXT NOT NULL,
    client_id     TEXT NOT NULL,
    action        TEXT NOT NULL,      -- sync | delete | dry_run | error
    payload_json  JSONB NOT NULL,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 4.9 UI

- "Add to Calendar" CTA on the plan screen. Disabled until the
  user has connected Google.
- Click → preview modal with:
  - List of events that will be created (day, time, recipe).
  - Reminder times per event.
  - Toggle: primary vs. dedicated calendar.
  - "Sync" / "Cancel" buttons.
- After sync: inline "Synced ✓ on Apr 17 (12 events)" badge with
  "Remove from calendar" and "Re-sync" actions.
- Error state: "Couldn't write 1 event — please try again." with
  structured error message.

### 4.10 Plan-change handling

When a plan changes (recipes added, swapped via SPEC-010, modified
via SPEC-016, etc.) and the user had previously synced:

- The sync is marked **stale** (`synced_at < plan.updated_at`).
- UI shows "Plan changed — sync again?"
- We do **not** auto-resync. Silent writes to the user's calendar
  are exactly the kind of thing that erodes trust.

### 4.11 Failure modes

- Google API timeout → retry with exponential backoff; after 3
  attempts, surface a structured error; do not partial-write (use
  Google's batch mode to make writes atomic per event where
  available).
- Token expired / revoked → endpoint returns 401 with a
  `reconnect_required=true` flag; UI prompts reconnection.
- Rate limit hit → queue the sync via Temporal; notify when done.
- Event count > 100 (unlikely on a weekly plan) → batch into
  pages.

### 4.12 Privacy and security

- Calendar connections live in the integrations team's credential
  store; this spec does not persist OAuth tokens directly.
- Event descriptions contain recipe ingredients and nutrient
  numbers; this is user data on user's own calendar. Acceptable
  per the existing platform privacy posture.
- We never include the user's profile ID, email, or any client-
  specific PII in event descriptions.
- Audit log (`nutrition_calendar_sync_audit`) retains metadata,
  not event descriptions beyond a digest hash.

### 4.13 Observability

- `calendar.sync{result}` — `created | updated | deleted |
  no_op | error`.
- `calendar.events_per_sync` histogram.
- `calendar.token_expired_total` (watch for reconnect storms).
- `calendar.sync_duration_ms` histogram.
- Alerts: error rate > 5% in an hour → page integrations on-call.

### 4.14 Priority-grouped work items

| # | Item | Priority |
|---|------|----------|
| W1 | Module scaffolding, version, types | P0 |
| W2 | Profile additions (`MealtimeWindow`, `calendar_buffer_minutes`, `prefer_dedicated_calendar`) + migration | P0 |
| W3 | Migration `013_calendar_sync.sql` + schema registration | P0 |
| W4 | `builder.py` plan → event drafts; timezone handling tests | P0 |
| W5 | `reminder.py` math + tests (including courtesy reminder) | P0 |
| W6 | `google_adapter.py` wrapper over integrations client | P0 |
| W7 | `sync.py` idempotent upsert + extendedProperties matching | P0 |
| W8 | `/sync` + `/sync DELETE` + dry-run support | P0 |
| W9 | Dedicated-calendar mode | P1 |
| W10 | Plan-change staleness detection | P1 |
| W11 | UI: "Add to Calendar" preview modal | FE | P1 |
| W12 | UI: synced state + resync / remove CTAs | FE | P1 |
| W13 | Mealtime editor in preferences UI | FE | P1 |
| W14 | Error handling: reconnect flow + retry | P1 |
| W15 | Observability counters + alerts | P1 |
| W16 | Benchmarks: sync p99 ≤ 5 s for 14 events | P2 |

---

## 5. Rollout Plan

Flag `NUTRITION_CALENDAR_SYNC` (off → UI hidden; on → surfaced).

### Phase 0 — Foundation (P0)
- [ ] W1–W7 landed. Migration in staging. Flag off.

### Phase 1 — Internal dogfood (P0)
- [ ] W8, W10 landed.
- [ ] Flag on internal. Team members sync their real plans to
      their real calendars.
- [ ] Acceptance gate: zero duplicate events, zero incorrect
      reminder times over 1 week of dogfood.

### Phase 2 — UI + preferences (P1)
- [ ] W11–W14 landed.
- [ ] Reviewer: UX copy reviewed (preview modal text, reconnect
      messaging).

### Phase 3 — Ramp (P1)
- [ ] 10% → 50% → 100% over two weeks.
- [ ] Watch: sync error rate, token-expired rate, UI "Add to
      Calendar" click-through.

### Phase 4 — Cleanup (P1/P2)
- [ ] W9 dedicated calendar.
- [ ] W15, W16 observability + benchmarks.

### Rollback
- Flag off → UI hidden. Previously synced events **remain in
  users' calendars** — we do not silently wipe. Users can
  manually remove via calendar UI or hit `DELETE` before
  rollback.
- Migration additive.

---

## 6. Verification

### 6.1 Unit tests

- `test_reminder_math.py` — prep 15 + cook 25 + buffer 10 =
  reminder at 50 min before mealtime.
- `test_timezone.py` — profile tz=America/New_York produces event
  localized correctly; DST transition week handled.
- `test_event_description.py` — description template stable;
  plan_id anchor appears exactly once.
- `test_builder_missing_times.py` — recipe without prep/cook times
  produces event with "(timing estimated)" and conservative
  defaults.

### 6.2 Integration tests (against Google API mock)

- `test_idempotent_sync.py` — two consecutive syncs produce the
  same event set; second sync is no-op.
- `test_sync_diff.py` — plan with one new recipe and one swapped
  recipe diffs correctly (1 create, 1 update, 0 delete).
- `test_delete.py` — DELETE removes all plan events and only those.
- `test_dedicated_calendar.py` — first sync creates "Khala Meals"
  calendar; subsequent syncs reuse.
- `test_reconnect_flow.py` — revoked token returns 401 with
  reconnect flag; after reconnect a retry succeeds.
- `test_rate_limit.py` — simulated 429 triggers Temporal-backed
  background sync.

### 6.3 Staleness tests

- `test_plan_change_marks_stale.py` — editing the plan sets
  `synced_at < plan.updated_at`; UI shows stale badge; resync
  clears staleness.

### 6.4 Observability

All §4.13 counters emit in staging. Alerts routed to on-call
test channel during Phase 2.

### 6.5 Cutover criteria

- All P0 tests green.
- Phase 1 dogfood acceptance met.
- Phase 3 ramp: sync error rate < 2%; reconnect prompts visible
  and working.
- Integrations team sign-off on reuse of their Google client.

---

## 7. Open Questions

- **Event colors.** Google Calendar color is a limited palette; we
  could color-code by meal type or leave user's default. v1 leaves
  default; could add a toggle later.
- **Past recipes.** Syncing a plan whose days include past dates
  would create past events — silly. v1 skips any recipe whose
  mealtime is in the past; the UI notes this on the preview.
- **Multi-language event templates.** v1 English only. The event
  description template is i18n-ready (keyed format string) but no
  translations in v1.
- **Granular mealtime by weekday (lunch at 13:00 on Mondays,
  12:00 otherwise).** Supported by `MealtimeWindow.day_override`
  but v1 UI only exposes the base schedule; overrides editable
  via API.
- **Fine-grained reminder customization (e.g. "no courtesy
  reminder on weekends").** Out of scope; add in v1.1 with a
  preference bundle.
