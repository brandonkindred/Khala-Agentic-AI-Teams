# Spec Clarification Chat Agent + Web UI Execution Plan

## Goal
Create a user-facing chat experience where a **Spec Clarification Agent**:

1. Accepts an initial product/engineering specification.
2. Asks targeted clarifying questions when there are unresolved assumptions, ambiguities, or missing acceptance criteria.

Alongside chat, provide a live execution dashboard showing:

- Current status and owner agent for in-flight work.
- Plan and status for each task.
- Progress bar for plan completion.
- Loop metrics (low/high/average loops per task).
- Task timing metrics (duration, started_at, finished_at).

## 1) Product Requirements (what to build)

### 1.1 Chat workflow
- User submits initial spec (text or file content).
- Agent returns:
  - understanding summary,
  - detected open questions,
  - assumptions currently being made,
  - first clarifying question.
- User answers in ongoing conversation.
- Agent iterates until either:
  - confidence threshold is reached, or
  - max clarification rounds reached.
- Agent produces a **Refined Spec** and a **Question Resolution Log**.

### 1.2 Transparency + planning dashboard
Display, in near real-time:
- **Now Working On**: task name, assignee agent, status, latest event timestamp.
- **Task Plan Table**:
  - task_id,
  - title,
  - assigned_agent,
  - status (`pending`, `in_progress`, `blocked`, `in_review`, `done`),
  - dependencies,
  - percent_complete.
- **Progress Indicator**: single bar from 0–100% across all tasks (weighted by task completion).
- **Loop Metrics** per task:
  - `loop_count_min`,
  - `loop_count_max`,
  - `loop_count_avg`.
- **Timing Metrics** per task:
  - `started_at`,
  - `finished_at`,
  - `duration_seconds`.

## 2) System Design

### 2.1 New/updated components

1. **Spec Clarification Agent** (new)
   - Input: initial spec + prior chat turns + current open questions.
   - Output: assistant message + structured payload:
     - open_questions,
     - assumptions,
     - confidence,
     - done_clarifying flag.

2. **Planning/Execution Tracker** (new shared module)
   - Central event store for task lifecycle and loop counters.
   - Computes derived metrics for UI.

3. **API Extensions** (new endpoints / websocket)
   - Start clarification session.
   - Send user message / receive assistant response.
   - Subscribe to live status updates.
   - Fetch task plan snapshot and metrics.

4. **Web UI** (new page/components)
   - Chat panel.
   - Active work/status panel.
   - Task plan table.
   - Progress bar.
   - Metrics cards/charts.

### 2.2 Data model (proposed)

```text
ClarificationSession
- session_id: str
- spec_text: str
- created_at: datetime
- status: active|completed
- refined_spec: str | null

ClarificationTurn
- session_id: str
- turn_index: int
- role: user|assistant
- message: str
- timestamp: datetime

ClarificationState
- session_id: str
- open_questions: list[str]
- assumptions: list[str]
- confidence_score: float
- clarification_round: int
- max_rounds: int

TaskStatus
- task_id: str
- title: str
- assigned_agent: str
- status: pending|in_progress|blocked|in_review|done
- dependencies: list[str]
- percent_complete: float
- loop_counts: list[int]   # per internal iteration sample
- started_at: datetime | null
- finished_at: datetime | null
```

Derived metrics:
- `loop_count_min = min(loop_counts)`
- `loop_count_max = max(loop_counts)`
- `loop_count_avg = sum(loop_counts)/len(loop_counts)`
- `duration_seconds = finished_at - started_at`

## 3) API Contract (proposed)

### 3.1 REST

- `POST /clarification/sessions`
  - body: `{ spec_text: string }`
  - returns: `{ session_id, initial_assistant_message, open_questions, assumptions }`

- `POST /clarification/sessions/{session_id}/messages`
  - body: `{ message: string }`
  - returns: `{ assistant_message, open_questions, assumptions, done_clarifying, refined_spec? }`

- `GET /clarification/sessions/{session_id}`
  - returns full session snapshot.

- `GET /execution/tasks`
  - returns task plan + status + derived metrics.

### 3.2 Realtime channel

- `GET /execution/stream` (SSE) or `WS /execution/ws`
  - Emits events:
    - `task_started`,
    - `task_progress`,
    - `task_loop_observed`,
    - `task_finished`,
    - `task_blocked`,
    - `plan_progress_updated`.

Recommendation: start with **SSE** for simplicity and easy browser support.

## 4) UI Plan

### 4.1 Layout
- **Left column**: Chat transcript + input composer.
- **Right column**:
  1. Current Work card,
  2. Plan progress bar,
  3. Task table,
  4. Metrics cards for loops and timing.

### 4.2 Components
- `ChatThread`
- `ChatComposer`
- `CurrentWorkCard`
- `PlanProgressBar`
- `TaskPlanTable`
- `LoopMetricsCard`
- `TaskTimingCard`

### 4.3 UX behavior
- Auto-scroll chat on new assistant/user message.
- Optimistic rendering for sent user message.
- Streaming updates to task status without page refresh.
- Highlight blocked tasks with remediation hint.

## 5) Execution Logic

### 5.1 Clarification stop conditions
Stop asking questions when one of these is true:
- no unresolved critical ambiguities,
- confidence score >= threshold (e.g., 0.85),
- max rounds reached.

When done:
- emit `clarification_completed`,
- persist `refined_spec` and `question_resolution_log`.

### 5.2 Progress computation
- Baseline: equal-weight per task completion (`done/total`).
- Optionally upgrade to weighted by complexity points.

### 5.3 Loop metric capture
Each agent iteration emits loop events:
- `loop_index`,
- `task_id`,
- `agent`,
- `reason` (e.g., validation failure, dependency wait, rework).

Aggregator updates min/max/avg loop metrics in-memory + persistence.

## 6) Milestones

### Milestone 1 — Backend foundations (2–3 days)
- Clarification session models.
- Session + message APIs.
- Task tracker schema and in-memory store.
- Unit tests for metrics derivation.

### Milestone 2 — Realtime telemetry (1–2 days)
- SSE endpoint.
- Event emission from orchestrator/task runner.
- Integration tests for stream ordering and reconnect behavior.

### Milestone 3 — UI implementation (2–4 days)
- Chat components and session handling.
- Status panel, progress bar, task table.
- Loop/timing metric cards.
- Loading/skeleton states and error banners.

### Milestone 4 — Hardening (1–2 days)
- E2E happy path (spec -> clarifications -> refined spec).
- Edge cases (no questions, user non-response, blocked tasks).
- Performance pass and accessibility checks.

## 7) Acceptance Criteria
- User can start a session with a spec and receive clarifying questions.
- Conversation remains visible as an ongoing chat transcript.
- UI shows real-time task ownership/status by agent.
- UI shows full plan and per-task status.
- Progress bar reflects overall completion.
- UI shows loop min/max/avg metrics per task.
- UI shows start/end timestamps and duration per task.
- Refined spec is produced at clarification completion.

## 8) Risks and mitigations
- **Risk:** Noisy loop data from inconsistent event emission.
  - **Mitigation:** enforce event schema + validation in tracker.
- **Risk:** Chat can stall with repetitive questions.
  - **Mitigation:** deduplicate question intents; maintain asked-question memory.
- **Risk:** Realtime channel disconnects.
  - **Mitigation:** SSE retry + periodic snapshot refresh fallback.

## 9) Immediate Next Steps
1. Implement `ClarificationSession` and `TaskStatus` models in backend.
2. Add session create/message/snapshot endpoints.
3. Add SSE execution stream endpoint and event publisher hooks.
4. Build UI shell with chat + progress + plan table + metric cards.
5. Wire frontend to backend snapshot + stream APIs.
