# Software Engineering Team — UX & Accessibility Audit

Produced by running [`docs/prompts/ux-accessibility-review.md`](../prompts/ux-accessibility-review.md)
(TEAM = Software Engineering Team) rather than a generic checklist, so findings are
scoped to this codebase's actual routes, tokens, shared primitives, and existing test
coverage. WCAG 2.2 AA is the compliance bar; AAA/best-practice opportunities are called
out separately and never held against AA.

## Pages reviewed

From `user-interface/src/app/app.routes.ts`, the four routes owned by Software
Engineering:

| Route | Component |
|---|---|
| `/software-engineering` | `SoftwareEngineeringDashboardComponent` |
| `/software-engineering/planning` | `PlanningPageComponent` |
| `/software-engineering/coding-team` | `CodingTeamPageComponent` |
| `/software-engineering/code-review` | `CodeReviewDashboardComponent` |

## Scoping correction: 11 of the "SE" components on disk are dead code

The `components/` directory holds several more SE-flavored components than the four
routes above actually use: `run-team-form`, `run-team-tracking`, `execution-tasks`,
`execution-stream`, `job-status`, `planning-job-status`, `product-analysis-job-status`,
`product-analysis-run-form`, `backend-code-v2-job-status`, `frontend-code-v2-job-status`,
and `start-from-spec-form`. Every one of them was traced by import chain from the four
routes and by a repo-wide grep for its class name/selector — **none has a live
consumer**:

```bash
grep -rl "RunTeamFormComponent\|app-run-team-form" user-interface/src/app --include=*.ts --include=*.html
grep -rl "ExecutionTasksComponent\|app-execution-tasks" user-interface/src/app --include=*.ts --include=*.html
grep -rl "import.*JobStatusComponent.*from.*job-status/job-status" user-interface/src/app --include=*.ts
# etc. — all empty except the component's own files
```

These are leftovers from an earlier synchronous "run-team" upload flow that predates
the current async job / GitHub-issue-driven Planning → Coding Team → Code Review
pipeline. They are **not user-facing accessibility findings** (a page a user cannot reach
has no user-facing consequence), but they are worth a decision: delete them, or — if
they're slated for reconnection — fix them first. `src/styles/scss-contrast-guard.spec.ts`
lints their SCSS unconditionally today even though nothing renders it, and
`product-analysis-run-form.component.scss:1,6` has hardcoded non-token colors
(`#7b1fa2`, `#e0e0e0`) that would need cleanup before reconnecting. Filed as **CLARITY-3**
below.

The live component tree actually reviewed:

- `/software-engineering` → `DashboardShellComponent`, `TeamAssistantChatComponent`
- `/software-engineering/planning` → `HealthIndicatorComponent`, `TeamAssistantChatComponent`
- `/software-engineering/coding-team` → `HealthIndicatorComponent`, `CodingTeamMonitorComponent`,
  `TeamAssistantChatComponent`, `OutOfScopeIssuesComponent`, `PendingQuestionsComponent` → `QuestionCardComponent`
- `/software-engineering/code-review` → `HealthIndicatorComponent`, `LoadingSpinnerComponent`,
  `EmptyStateComponent`, `InlineBannerComponent`, `PrReviewDetailComponent` →
  `CodeReviewTranscriptDialogComponent`, `CodeReviewSystemicFindingsDialogComponent`
- Global chrome on all four: `AppShellComponent` → `BreadcrumbComponent`,
  `InitialsAvatarComponent`, `ApiStatusWidgetComponent`

Note also: `DashboardShellComponent` — the shared page-header/sub-nav wrapper — is the
repo-wide dashboard convention, imported by 17 components across the app (Blogging,
Market Research, Social Marketing, SOC2, Sales, AI Systems, Job Matching, Startup
Advisor, Personal Assistant, Deepthought, Road Trip, Accessibility, Agent Studio, User
Profile and the SE dashboard among them — `grep -rl DashboardShellComponent
user-interface/src/app --include=*.ts`). Of the four SE routes, only the top-level SE
dashboard uses it; Planning, Coding Team, and Code Review each hand-roll their own
`.kh-page-header` instead. So those three deviate from a **repo-wide** convention, not an
SE-local one — which strengthens rather than weakens A11Y-6's consistency argument. One
nuance in the other direction: only the SE dashboard passes the `subTeams` input today,
so the `aria-current` half of A11Y-6 has an SE-only blast radius at present even though
the component itself is shared app-wide. This has real consequences below
(no "Sub-teams" nav on Planning; no `aria-current` anywhere in that nav pattern).

## Primary task walkthrough

**Task: take a spec from idea to a merged PR.**

1. Land on `/software-engineering` (empty state) → click **New Project** (1 click).
2. In the revealed chat/form split view, either type a spec into the chat, or click
   each of the 3 form fields (Project specification*, Tech stack, Constraints) and
   type directly (3 clicks + typing, or conversational — either path is fine).
3. Click **Launch workflow** once all required fields are filled (1 click).
4. The job now runs unattended through Planning → Coding Team (Tech Lead + Task Graph)
   → Code Review. To check on it: navigate to **Coding Team → Jobs** (2 clicks: global
   nav flyout → Coding Team, then the Jobs toggle — Jobs is actually the page's default
   view, so often 1 click) and expand the running run (1 click) to see live
   `coding-team-monitor` progress.
5. If the Tech Lead has a clarifying question, `PendingQuestionsComponent` renders
   inline on the Coding Team page — answer and **Submit All Answers** (1+ clicks).
6. Once code is pushed, go to **Code Review** (1 click via global nav — **not**
   reachable from the Coding Team page's own chrome, see A11Y-6) and expand the PR row
   to watch the review run and read findings (1 click).

**Step count for the golden path (spec → launched job): 5 interactions** (New Project,
fill 3 fields, Launch). That's tight and reasonable — the interaction-cost lens has no
finding against the launch flow itself. The friction in this audit is almost entirely
in *accessibility of those same interactions* (can a keyboard/screen-reader user
actually complete step 2's field-filling; can they tell a run failed silently) rather
than in the step count.

## Top 5

1. **STATE-1** — The SE dashboard's job-list poll has no error handler; the first
   failed request kills it permanently and the page never recovers without a reload.
2. **A11Y-1** — The global error toast's text is ~1.03:1 contrast (needs 4.5:1) —
   effectively unreadable, on the one page (SE dashboard/Planning) where it's the only
   error channel.
3. **A11Y-2** — The "New Project" form's three field buttons have no field-identifying
   accessible name; a screen-reader user hears the same announcement for all three.
4. **A11Y-3** — The only escape hatch out of "GitHub not configured" (Code Review *and*
   Coding Team) is a link at 1.18:1 contrast with no underline — effectively invisible.
5. **FLOW-1** — Focus never moves when New Project reveals the form, or when a
   Coding-Team run/issue row expands — cheap fix (`defer-focus.ts` exists exactly for
   this), hit on every single use of the primary task.

## State dispositions

### `/software-engineering` (SE dashboard)

All `.ts:` / `.html:` citations in this table refer to
`software-engineering-dashboard.component.ts` / `.html`.

| State | Disposition | Evidence |
|---|---|---|
| first visit | HANDLED | `activeView` starts `'empty'`; empty-state renders immediately. `software-engineering-dashboard.component.ts:38` |
| empty | HANDLED | Same branch; stays `'empty'` if `allJobs.length === 0` after the poll resolves. `.ts:56-58` |
| first load | MISSING | No loading state while the first `getRunningJobs` call is in flight — the empty state renders confidently before data arrives, with no spinner/skeleton. Scoped search: no `loading` field anywhere in this component. `.ts:34-65` |
| refresh | HANDLED (partial) | `timer(0, POLL_JOBS_MS)` (`POLL_JOBS_MS = 30_000`, `.ts:15`) re-polls and updates the lists in place — but only while every request succeeds. The poll has no error handler and dies permanently on the first failure, so refresh is robust only on the happy path (see STATE-1). `.ts:49-55` |
| partial | N/A | Single all-or-nothing `getRunningJobs()` call; nothing assembles a partial view from multiple calls. |
| populated | HANDLED | Running/Completed sections render job rows. `.html:55-102` |
| long-running | MISSING | The page owns no per-job progress and its intended handoff is broken: both job rows link to `['/jobs', job.job_id]` (`.html:63,81`), but `app.routes.ts` defines no `jobs/:jobId` route (the only job-detail path is `blogging/jobs/:jobId/artifacts/:artifactName`), so the `{ path: '**', redirectTo: '/dashboard' }` wildcard swallows it. The user is bounced to the generic Jobs Dashboard with no job context. Filed as STATE-2. |
| stalled | MISSING | Same broken handoff — and no stall affordance of its own. |
| failed | HANDLED (partial) | A terminal `failed`/`stopped` job still renders in "Completed" with its status text (`status-{{job.status}}`), but why it failed is not shown, and the row's link to a per-job view does not resolve (STATE-2). `.html:76-92` |
| permission-denied | MISSING | See STATE-1 below. |
| backend-unconfigured | MISSING | See STATE-1 below. |
| offline | MISSING | See STATE-1 below. |
| API-error | MISSING | See STATE-1 below. |
| stale-data | MISSING | See STATE-1 below. |

### `/software-engineering/planning`

| State | Disposition | Evidence |
|---|---|---|
| first visit | HANDLED | Static header + health check + chat render immediately. `.html:1-21` |
| empty / populated | N/A | Page has no list of its own; it's a single persistent form/chat surface. Its data-fetch states live inside `team-assistant-chat` (a shared primitive, reviewed once under A11Y-2/A11Y-4/FLOW-1 rather than per host page). |
| first load | HANDLED | `app-health-indicator` starts in a "Checking…" state until the check resolves. `health-indicator.component.ts` |
| refresh | N/A | No periodic refresh owned by this page beyond the health ping. |
| partial | N/A | No multi-call assembly. |
| long-running / stalled | N/A | A launched job hands off entirely to the SE dashboard's Jobs list; this page only shows a confirmation snackbar. `.ts:37-41` |
| failed | HANDLED (delegated) | Launch failure surfaces through `team-assistant-chat`'s own error branch + the global toast. |
| permission-denied / backend-unconfigured / offline / API-error | HANDLED, degraded | Same delegation — and this is the only page whose errors are *handled* yet still terminate at the global toast with no inline banner of its own, so "handled" is functionally close to "handled unreadably." The SE dashboard is toast-only too, and worse: there the poll dies outright, which is filed separately as STATE-1. Cross-referenced under A11Y-1, not filed again here. |
| stale-data | N/A | No cached "data" beyond the health check, which re-checks on its own cadence. |

### `/software-engineering/coding-team` (Jobs/Runs panel — the page's default view)

| State | Disposition | Evidence |
|---|---|---|
| first visit | HANDLED | Page opens on the Jobs/Runs panel by default. `.ts:165` |
| empty | HANDLED | "No runs yet" with a next action ("Open the GitHub view…"). `.html:380-385` |
| first load | MISSING | `runs.length === 0` gates the same empty-state branch used for steady-state empty (`coding-team-page.component.html:380`); no distinct loading affordance for the panel's first fetch. Scoped search: no loading flag guards the empty branch. |
| refresh | HANDLED | `timer(0, RUNS_POLL_MS)` with `catchError` at the inner observable, so a failed poll can't kill the outer chain. `.ts:917-934` |
| partial | N/A | Single `listJobs()` call feeds the whole panel. |
| populated | HANDLED | Running/Recent sections. `.html:386+` |
| long-running | HANDLED | Expanding a running run shows live `coding-team-monitor` progress. `.html:499` |
| stalled | MISSING | `shared/stall-warning` exists in the codebase but is imported only by the two dead components (`run-team-tracking`, `job-status`) — **not used anywhere live**, despite this panel's polling being exactly the kind of long-running-job surface it exists for. Scoped search: `grep -rl StallWarning src/app/components/coding-team-page src/app/components/coding-team-monitor` → no match. |
| failed | HANDLED | Failed runs show in Recent with a status badge that always pairs the colour class with the literal status text — `<span class="kh-badge kh-badge--{{ vm.badgeClass }}">{{ vm.status }}</span>`, so the status is never colour-only. `coding-team-page.component.html:434` (and `:482` for the expanded run's badge) |
| permission-denied / backend-unconfigured | HANDLED | GitHub-not-configured renders a clear message + link (marred by A11Y-3's contrast). `.html:66-73`. A `listJobs()` 401/403 lands in `runsError` → `<app-inline-banner variant="error">`, which is `role="alert"`. `.ts:927-930`, `.html:377` |
| offline / API-error | HANDLED | Same `runsError`/inline-banner path. It gets the part STATE-1's page gets wrong — the poll survives the failure — but not last-good retention: see the `stale-data` row. |
| stale-data | MISSING | The panel does not go stale on a failed poll; it goes **empty**. `catchError` returns `of([] as CodingTeamJobListItem[])` (`coding-team-page.component.ts:931-934`), the subscription feeds that into `applyRuns` (`:939`), and `applyRuns` assigns `this.runs = mine` unconditionally (`:969`) — so a single failed poll clears the list and re-renders the `runs.length === 0` empty state (`coding-team-page.component.html:380`) beneath the error banner. There is no "as of" timestamp and no retained last-good list, so the user cannot tell how old the view is; they are shown "No runs yet" for runs that exist. |

### `/software-engineering/code-review`

| State | Disposition | Evidence |
|---|---|---|
| first visit | HANDLED | Checks GitHub config, then loads repos. `.html:22-28` |
| empty | HANDLED | Per-branch empty states ("No repository access", "No open pull requests", etc.), each a real `h3` via `EmptyStateComponent`'s `headingLevel` input. `.html:57-62,79-84,133,155-160` |
| first load | HANDLED | Uses the shared `app-loading-spinner` (`role="status" aria-live="polite" aria-busy="true"`), unlike Coding Team's hand-rolled spinners — see CLARITY-2. |
| refresh | HANDLED | Refresh button + resilient polling; `reviewAnnouncement` is reset via a `timer(0)` tick specifically so two consecutive identical announcements still get spoken. `.ts:138-151` |
| partial | N/A | Single-call-per-view pattern. |
| populated | HANDLED | Repo rows and, once a repo is expanded, its PR rows. `code-review-dashboard.component.html:89` (`cr-repo-row`), `:166` (`cr-pull-row`) |
| long-running | HANDLED | A running review's badge renders a spinner beside the status text rather than replacing it — the `mat-spinner` at `code-review-dashboard.component.html:185` sits inside the `cr-row-badge` (`:181`) whose `{{ friendly }}` label (`:189`) and `aria-label` (`:182`) still carry the status in words. The per-run table repeats this at `pr-review-detail/pr-review-detail.component.html:71-77`. |
| stalled | MISSING | Same gap as Coding Team — `shared/stall-warning` not used here either. |
| failed | HANDLED | Status badge with a full human-readable mapping table for every backend status, including a safe fallback. `review-metrics.ts:145-236` |
| permission-denied / backend-unconfigured | HANDLED | GitHub-not-configured empty state (marred by A11Y-3). For the repo/PR fetches themselves, confirmed `error:` handlers exist on every subscribe (not merely inferred): `.ts:181,207,270`. |
| offline / API-error | HANDLED | Same confirmed `error:` handlers. |
| stale-data | HANDLED | Unlike Coding Team, this page genuinely retains the last-good list: the `error:` handler at `code-review-dashboard.component.ts:207-210` sets `repoError` and clears the loading flag but never touches `this.repos`, so the previously loaded rows stay rendered beneath the inline error. No "as of" timestamp, but the error is the staleness signal and the data behind it is real. This is the better of the two in-team patterns and the one STATE-1's fix should be measured against. |

## Findings

### STATE-1 — SE dashboard's job list dies permanently on the first API error
- **Severity**: High
- **Location**: `user-interface/src/app/components/software-engineering-dashboard/software-engineering-dashboard.component.ts:48-61`, backed by `services/software-engineering-api.service.ts:60-66` (`getRunningJobs` is a bare `http.get` passthrough with no internal `catchError`).
- **What the user hits**: The very first `/run-team/jobs` request that fails (a
  transient 500, a timeout, a backend restart) throws inside an RxJS `subscribe({ next
  })` that has no `error` handler. RxJS treats an unhandled error as terminal: the
  `timer(0, POLL_JOBS_MS)` polling subscription dies right there. From that point on the SE
  dashboard's Jobs list is frozen — no more polling, ever, for the life of the page —
  with the only evidence being one 6-second global toast (itself unreadable per
  A11Y-1). A user who steps away and comes back sees stale or perpetually-empty data
  and has no way to know why, short of a full page reload.
- **Why it's a problem**: this is the primary landing surface for the SE team's whole
  job list; silently breaking it defeats the entire "hand it a spec, get a merged PR"
  promise the dashboard's own subtitle makes. Compare with
  `coding-team-page.component.ts:917-940`, which polls the *same kind* of job list and
  gets the survival half right — `catchError` on the **inner** observable keeps the outer
  `timer` alive and surfaces `runsError` via an inline `role="alert"` banner. That is the
  part to copy, and it is exactly what the SE dashboard lacks.
- **Recommended change**: adopt Coding Team's inner-`catchError` structure, then go one
  step further than it does on the error path. Be precise about what is house pattern and
  what is new here, because the two differ:
  - **Copied from Coding Team**: `catchError` inside the `switchMap` so the inner
    observable completes and the outer `timer` survives, plus a `tap` that clears the
    error only on success, plus an inline `role="alert"` banner.
  - **A deliberate refinement, not a copy**: emitting `EMPTY` instead of an empty
    payload. Coding Team returns `of([] as CodingTeamJobListItem[])`
    (`coding-team-page.component.ts:931-934`), which flows into `applyRuns` and assigns
    `this.runs = mine` unconditionally (`:969`) — so its panel *clears* on a failed poll
    and shows "No runs yet" under the error banner. Copying that here would blank the SE
    job list on any transient failure, telling the user they have no jobs when they do.
    `EMPTY` completes the inner observable without emitting, so `next` never runs and the
    last-good list stays on screen behind the banner.
  If last-good retention is the behavior the house wants, `coding-team-page`'s own
  `catchError` needs the same change — that is a separate, unfiled question this audit
  raises rather than answers. The fix below applies to the SE dashboard only:
  ```ts
  this.jobsSub = timer(0, POLL_JOBS_MS).pipe(
    switchMap(() => this.api.getRunningJobs(false).pipe(
      tap(() => { this.jobsError = null; }),
      catchError((err) => {
        this.jobsError = extractErrorDetail(err, 'Failed to load jobs.');
        return EMPTY;           // completes the inner observable; outer timer survives,
      }),                       // next never runs, so the last-good list is retained
    )),
  ).subscribe((resp) => { /* existing next logic, unchanged */ });
  ```
  Render `jobsError` through the shared `<app-inline-banner variant="error">`, placed
  outside the three `activeView` branches so it is visible in the `'empty'` view too —
  and note `InlineBannerComponent` is not currently in this component's `imports` array,
  so it must be added.
- **Cost**: S, this page only (the fix pattern already exists elsewhere in the same
  team).
- **Verification**: extend `software-engineering-dashboard.component.spec.ts` (or add
  `.a11y.spec.ts` — neither currently calls `expectNoAxeViolations` for this component)
  with a test that makes `getRunningJobs` error once, then asserts a second poll tick
  still fires and `jobsError` renders.

### STATE-2 — Every job row on the SE dashboard is a dead link
- **Severity**: High
- **Location**: `user-interface/src/app/components/software-engineering-dashboard/software-engineering-dashboard.component.html:63` and `:81`; route table at `user-interface/src/app/app.routes.ts` (no `jobs/:jobId` entry; wildcard at `:316`); the established pattern at `user-interface/src/app/components/jobs-dashboard/jobs-dashboard.component.ts:704-719`.
- **What the user hits**: a user opens the Jobs view, sees their Running and Completed
  jobs, and clicks one to see what it's doing. Both sections render the row as
  `<a class="job-item" [routerLink]="['/jobs', job.job_id]">`, but **no `jobs/:jobId`
  route exists** — `app.routes.ts` defines only `blogging/jobs/:jobId/artifacts/:artifactName`
  as a job-detail path, so `{ path: '**', redirectTo: '/dashboard' }` at `:316` catches
  the navigation and dumps the user on the generic Jobs Dashboard with no job context
  and no explanation. Every row in the list behaves this way, in both sections. Verified
  by grep: these two lines are the only `'/jobs'` router links in the entire app.
- **Why it's a problem**: this is not primarily a WCAG failure — it is a broken primary
  navigation path, and it fails worst for the users this audit is about. A screen-reader
  or keyboard user has no visual cue that they were silently redirected rather than
  taken to a detail view, so the "nothing happened, or did it?" recovery cost is highest
  for them. It also invalidates a chunk of this audit's own reasoning: the earlier draft
  of the `/software-engineering` state table dispositioned `long-running` and `stalled`
  as N/A *because* the page "hands off to `/jobs/:jobId`". That handoff does not exist,
  so both rows are now MISSING and this page owns states it appeared to delegate.
- **Recommended change**: adopt the repo's existing per-job navigation pattern rather
  than inventing a route. `jobs-dashboard.component.ts:704-719` (`navigateToJob`) routes
  to the owning team's page with the job id as a query param — for SE jobs,
  `this.router.navigate([info.route], { queryParams: { jobId, tab } })`. Replace the two
  `[routerLink]="['/jobs', job.job_id]"` bindings with the same shape so a job row lands
  on the SE surface that can actually show it. Adding a real `jobs/:jobId` route is the
  alternative, but it is the larger change and diverges from what the rest of the app does.
- **Cost**: S for the link fix (two bindings plus a handler), M if a genuine per-job
  detail route is preferred instead. This page only.
- **Verification**: a router-harness spec asserting that activating a job row navigates
  to a route that resolves — i.e. not the `**` wildcard. A keyboard walkthrough of the
  Jobs view confirms the row lands somewhere with the job's context.

### A11Y-1 — Global error toast text is ~1.03:1 contrast — effectively unreadable
- **Severity**: High (Blocker-level on Planning/SE-dashboard, where it's the only error channel)
- **Location**: `user-interface/src/styles.scss:447-453` (`.mat-mdc-snack-bar-container .mdc-snackbar__surface` overrides only `background`/`border`, never a text-color token) + `error-handler.interceptor.ts:52-62` (every API failure app-wide routes through this).
- **What the user hits**: reproduced live (Playwright + axe-core against the running
  dev build) on all four SE routes. axe: `.mdc-snackbar__label` measured **1.03:1**
  (foreground `#362f2b` on background `#333333`; needs 4.5:1) and the "Close" action
  measured **1.95:1** (`#964900` on `#333333`). Screenshot confirms it visually — the
  message text is indistinguishable from the background. Root cause: the app overrides
  the snackbar's *background* to the dark `--kh-surface-4` token but never overrides
  Angular Material's own default *text*-color tokens, so MDC's light-theme label/action
  colors are left sitting on the new dark background.
- **Why it's a problem**: WCAG 1.4.3 Contrast (Minimum), AA. This is the single
  highest-blast-radius finding in the audit — it's not SE-specific, it's every error
  toast in the entire app (also fired from `agent-runner.component.ts:417`) — but it's
  filed here because it's the *only* error surface on Planning and the SE dashboard
  (both lack an inline banner fallback), so it's compliance-relevant for this team's
  primary task specifically, not just a cosmetic nit elsewhere.
- **Recommended change**: add explicit text-color tokens to the snackbar override:
  ```scss
  .mat-mdc-snack-bar-container {
    .mdc-snackbar__surface { background: var(--kh-surface-4) !important; border: 1px solid var(--kh-border) !important; }
    .mdc-snackbar__label { color: var(--kh-text-primary) !important; }
    .mat-mdc-snack-bar-action .mdc-button__label { color: var(--kh-accent) !important; }
  }
  ```
  One fix at `styles.scss:447-453` clears this for every team, not just SE.
- **Cost**: S, shared primitive (global stylesheet).
- **Verification**: no jsdom spec can catch this — `color-contrast` is disabled outright
  under jsdom (`src/app/testing/a11y.ts`). Verification is a real-browser contrast
  measurement (repeat the axe-core-against-a-running-`ng serve` check used to find
  this) plus a visual snapshot.

### A11Y-2 — "New Project" field buttons have no field-identifying accessible name
- **Severity**: High
- **Location**: `user-interface/src/app/components/team-assistant-chat/team-assistant-chat.component.html:61-97`
- **What the user hits**: a screen-reader user tabs into the "New Project" form panel.
  Each of the three required fields (Project specification, Tech stack, Constraints) is
  a `<button class="field-value">` whose only content — and therefore only accessible
  name — is `"Click to fill in or chat with the assistant"` (line 94) when empty, or
  the raw filled value when filled (line 92). The visible field label
  (`<span class="field-label">`, line 63) sits in a **sibling** element, never inside
  the button and never wired via `aria-labelledby`. Three sequential Tab stops
  therefore announce **the identical string** three times — there is no way to tell
  which field is which without sight. This is confirmed by reading the template
  directly (axe doesn't catch it: the button has *a* name, just not a useful one, so
  no rule fires).
- **Why it's a problem**: WCAG 4.1.2 Name, Role, Value (A) / 2.4.6 Headings and Labels
  (AA) — the accessible name must identify the control's purpose, and here it doesn't.
  Blast radius: `team-assistant-chat` is the shared "describe your project" primitive
  reused by Planning, Coding Team's Chat tab, the SE dashboard's New Project view, and
  — per this component's own reuse pattern — reportedly a dozen-plus other teams'
  dashboards. One fix clears the same defect everywhere it's embedded.
- **Recommended change**: give the row's label an id, give the button its own id, and
  reference **both** from the button:
  ```html
  <!-- uid is a per-instance prefix on the component, e.g. a readonly field
       initialised from an incrementing counter — field.key alone is only unique
       within one mounted instance (see the uniqueness note below). -->
  <span class="field-label" [id]="uid + '-field-label-' + field.key">…</span>
  ...
  <button class="field-value"
          [id]="uid + '-field-value-' + field.key"
          [attr.aria-labelledby]="uid + '-field-label-' + field.key + ' ' + uid + '-field-value-' + field.key"
          (click)="startEdit(field.key)">
  ```
  Referencing the button's own id is load-bearing, not belt-and-braces: `aria-labelledby`
  **replaces** name-from-content rather than appending to it (the `aria-labelledby` step of
  the accname-1.2 computation, which returns without falling through to name-from-content),
  so a list naming only the label span yields the accessible name "Project specification"
  and the field's value or placeholder is dropped entirely — a regression for filled
  fields, which announce their value today. Verified in Chromium via the CDP accessibility
  tree: `aria-labelledby="field-label-spec"` computes to `"Project specification"`, while
  `aria-labelledby="field-label-spec field-value-spec"` computes to
  `"Project specification Build a todo app in React"`. Because the button is repeated per
  field and the component can be mounted more than once per page, both ids must be unique
  per instance, not just per field key.
- **Cost**: S, shared primitive (huge blast radius, trivial fix).
- **Verification**: `team-assistant-chat.component.a11y.spec.ts` already exists — add an
  assertion that each field button's computed accessible name contains **both** its
  `field.label` and its current value (or the empty-state placeholder). Asserting only
  that the name contains `field.label` passes against the broken single-reference form
  too, so it would not catch the dropped-value regression.

### A11Y-3 — The only way out of "GitHub not configured" is a near-invisible link
- **Severity**: High
- **Location**: root cause at `user-interface/src/styles.scss:132-140` (global `a { color: var(--kh-accent-text); text-decoration: none; }`, hover-only color change, no compensating underline); reproduction sites at `user-interface/src/app/components/code-review-dashboard/code-review-dashboard.component.html:26` and `user-interface/src/app/components/coding-team-page/coding-team-page.component.html:71`.
- **What the user hits**: reproduced live — the Code Review page's "GitHub integration
  is not configured" state renders `<a routerLink="/integrations">Set up GitHub</a>`
  inline in a sentence. axe's `link-in-text-block` rule measured **1.18:1** contrast
  between the link (`--kh-accent-text`, `#fde68a`) and its surrounding text
  (`--kh-text-secondary`, `#d4d4d8`) — both pale colors on the dark theme, indistinguishable
  from each other, and the global `a` rule strips the underline that would otherwise
  compensate. A low-vision user, or anyone who can't rely on the (barely-there) hue
  shift, cannot tell this sentence contains a link at all. The Coding Team page's
  GitHub tab has the identical pattern for the identical message.
  This is the *only* control that gets a user unstuck from this state on either page.
- **Why it's a problem**: WCAG 1.4.1 Use of Color (A) — text alone must not be the only
  way a link is distinguished from surrounding text, and here the color difference
  itself is also below the 3:1 non-text-contrast bar axe checks for exactly this rule.
- **Recommended change**: this is a token-level fix, not a per-link one. Either restore
  underlines on inline links (`text-decoration: underline` in `styles.scss:134`, the
  simplest and most broadly-correct fix), or, if the underline-free look is
  intentional, raise `--kh-accent-text` so it clears 3:1 against `--kh-text-secondary`
  wherever the two can appear together, and add `text-decoration: underline` at minimum
  to links rendered inside body-copy paragraphs (exactly the `<p>` context both
  reproduction sites use).
- **Cost**: S, shared primitive (`styles.scss:132-140`) — one fix covers every inline
  link in the app, including these two SE-owned dead-ends.
- **Verification**: real-browser contrast check (as above) plus a visual regression
  snapshot of an inline link inside body text.

### A11Y-4 — "Edit manually" button is invisible when reached by keyboard
- **Severity**: Medium
- **Location**: `user-interface/src/app/components/team-assistant-chat/team-assistant-chat.component.scss:197-208`
- **What the user hits**: a keyboard-only sighted user tabs to the "Edit manually" icon
  button next to a filled field. It's `opacity: 0` by default and only reaches
  `opacity: 1` on `.form-field:hover` (line 200) — there is no `:focus-visible` or
  `:focus-within` rule, so a focused-but-not-hovered button is completely invisible,
  including its focus ring (opacity 0 hides the whole box). Functionally the user isn't
  blocked — clicking anywhere on the `.field-value` button itself (html:90) also opens
  edit mode — but a sighted keyboard user has no visual confirmation of where focus is
  for several tab stops in this panel, and may not discover the edit affordance exists
  at all. Contrast with the *correct* version of this exact pattern two components
  over: `app-shell.component.scss` — its `.nav-link-star` uses a non-zero baseline
  opacity plus an explicit `&:focus-visible { opacity: 1; }` rule.
- **Why it's a problem**: WCAG 2.4.7 Focus Visible (AA).
- **Recommended change**:
  This is an **additive** one-line change to the existing hover selector, not a
  replacement for the whole rule — leave every other declaration in `.edit-btn`
  (`team-assistant-chat.component.scss:197-208`) untouched:
  ```scss
  // in the existing .edit-btn rule, extend the hover selector on line 200:
  .form-field:hover &, &:focus-visible { opacity: 1; }
  ```
- **Cost**: S, shared primitive.
- **Verification**: manual keyboard walkthrough of the New Project / Planning / Coding
  Team chat forms; adding a `.a11y.spec.ts` assertion is not practical for computed
  `opacity` under jsdom — note as a manual check.

### A11Y-5 — Coding Team's phase stepper conveys progress by color alone
- **Severity**: Medium
- **Location**: `user-interface/src/app/components/coding-team-monitor/coding-team-monitor.component.html:33-46`, colors in `.component.scss:96,125-151`
- **What the user hits**: a screen-reader user on the live-progress panel of a running
  job hears "Planning / Coding / Completed" as three identically-presented items — the
  same fixed icon renders per step regardless of whether it's done, current, or failed;
  only CSS classes (`.completed/.current/.failed/.pending`) change color. There's no
  icon swap and no visually-hidden state text.
- **Why it's a problem**: WCAG 1.4.1 Use of Color (A).
- **Recommended change**: add a visually-hidden state suffix per step, e.g.
  `<span class="visually-hidden">{{ stepState === 'completed' ? '(completed)' : stepState === 'current' ? '(in progress)' : stepState === 'failed' ? '(failed)' : '(pending)' }}</span>`,
  or swap the icon itself (`check_circle`/`autorenew`/`error`/`radio_button_unchecked`)
  the way the (dead, but instructive) job-status components already do.
- **Cost**: S, this component (embedded on both Coding Team's Jobs panel and its own audit view).
- **Verification**: `coding-team-monitor.component.a11y.spec.ts` already exists — add
  an assertion on the stepper's accessible text per state.

### FLOW-1 — Focus never moves when the primary task's views change
- **Severity**: Medium
- **Location**: `software-engineering-dashboard.component.ts:67-73` (`showNewProject()`/`showJobs()`), `coding-team-page.component.ts` (issue-select and run-expand handlers that reveal inline panels without moving focus into them)
- **What the user hits**: reproduced live — clicking **New Project** swaps the entire
  page body from an empty-state message to a two-panel chat/form layout, but focus
  stays on the New Project button (confirmed via Playwright: `document.activeElement`
  is still the button after the click). A screen-reader user hears nothing change.
  Same pattern selecting a GitHub issue (reveals a "Start AI coding on this issue?"
  confirmation panel below the list) and expanding a run row (reveals its live detail)
  on the Coding Team page — a keyboard user must continue tabbing past the rest of a
  potentially long, paginated list to reach the newly-revealed content instead of
  landing in it directly.
- **Why it's a problem**: focus management lens — content is destroyed/replaced without
  a deliberate focus move. The codebase already has the right tool for this
  (`shared/defer-focus.ts`, "move focus after a re-render") and already uses the
  *route-level* version of this correctly (`app-shell.component.ts:139-148` moves focus
  to `#main-content` on every real navigation) — this finding is specifically about the
  *in-page* view-state transitions that route-level fix doesn't cover.
- **Recommended change**: call `deferFocus` (or equivalent) targeting the new panel's
  heading/first field when `activeView` flips to `'new-project'` (`showNewProject()`)
  and when it flips to `'jobs'` via the header's "Jobs (N)" button (`showJobs()`), and
  targeting the confirmation/detail panel's container when an issue is selected or a run
  is expanded. Move focus **only on the user-initiated transitions**: the poll also flips
  `activeView` from `'empty'` to `'jobs'` on its own once jobs appear
  (`software-engineering-dashboard.component.ts:56-58`), and stealing focus on a
  background timer would yank a user out of whatever they were reading. So the focus call
  belongs in the two click handlers, not in a setter on `activeView`.
- **Cost**: S–M, four call sites (`showNewProject`, `showJobs`, issue-select,
  run-expand), reusing an existing shared helper.
- **Verification**: add a spec assertion that after the state change, `document.activeElement` is inside the newly-rendered panel, not the trigger button.

### A11Y-6 — Sub-teams nav has no active-state indication, and omits Code Review
- **Severity**: Medium
- **Location**: `user-interface/src/app/shared/dashboard-shell/dashboard-shell.component.html:24-32` (no `aria-current`/`routerLinkActive` at all); `software-engineering-dashboard.component.html:5-8` (`subTeams` array lists only Planning and Coding Team)
- **What the user hits**: the SE dashboard's in-page "Sub-teams: Planning · Coding
  Team" strip has no `aria-current="page"` on the active link and no `.active` visual
  treatment either — contrast with the *global* nav, which does this correctly
  (`app-shell.component.html:22-23,38-39,96,129`, per `ACCESSIBILITY.md`'s own stated
  convention: "Navigation items use `aria-current="page"` for the active route").
  Separately, the same array is simply missing a third entry — Code Review is a sibling
  routed page (confirmed live via `navigation.model.ts:53`, where the global nav *does*
  list it) but isn't reachable from this in-page strip at all. A user following the
  page-level breadcrumb pattern has no path from the SE dashboard to Code Review except
  the global flyout nav.
- **Why it's a problem**: consistency (the house standard already states this rule and
  the global nav already follows it) and orientation — a returning user can't tell
  which sub-team page they're on from the strip itself, and the strip's own list is
  incomplete.
- **Recommended change**:
  ```html
  <a [routerLink]="team.route" routerLinkActive="active" #rla="routerLinkActive"
     [attr.aria-current]="rla.isActive ? 'page' : null">
  ```
  in `dashboard-shell.component.html`; add `{label: 'Code Review', route:
  '/software-engineering/code-review'}` to the `subTeams` array in
  `software-engineering-dashboard.component.html:5-8`; **and add an `.active` rule to
  `dashboard-shell.component.scss`**, which currently has none (`grep -n active
  user-interface/src/app/shared/dashboard-shell/dashboard-shell.component.scss` returns
  nothing). Without it, `routerLinkActive="active"` binds a class with no stylesheet
  behind it and the visual half of this finding stays unfixed — a sighted user still
  cannot tell which sub-team page they are on. Mirror the global nav's treatment in
  `app-shell.component.scss` (its `&.active` rules at `:122`, `:218`, `:246`, `:270`,
  `:353`) using `--kh-*` tokens rather than inventing a new accent.
- **Cost**: S, three parts: the `aria-current` binding and the new `.active` rule are
  both in the shared primitive (reaching every team dashboard that passes `subTeams`);
  the missing-link half is SE-specific.
- **Verification**: `dashboard-shell.component.spec.ts` — assert `aria-current="page"`
  is present when a `subTeams` route matches the current URL, and that the matching link
  carries the `active` class. The visual half needs an eyeball: a spec can prove the
  class is bound but not that the rule renders a perceptible difference.

### A11Y-7 — Supplementary info surfaced only via hover-only `matTooltip`
- **Severity**: Medium
- **Location**: `coding-team-page.component.html:160,166,252,256,371,439,506`; `code-review-dashboard.component.html:100`; `out-of-scope-issues.component.html:81,84,90`
- **What the user hits**: a sighted keyboard-only user cannot trigger a `matTooltip`
  placed on a plain `<span>`/`<div>`/`<mat-icon>` with no `tabindex` — the tooltip only
  opens on mouse hover. Most of the underlying text is duplicated elsewhere (so screen
  reader users aren't blocked), but a keyboard-only user gets nothing, and in two cases
  (`coding-team-page.component.html:256`'s "already working on this issue" chip,
  `coding-team-page.component.html:166`'s repo issue-count)
  the tooltip is the *only* place that information appears. The codebase already has
  the correct pattern one component over: `health-indicator.component.html:1-7` adds
  `tabindex="0"` to its `role="img"` span specifically so its tooltip is
  keyboard-reachable.
- **Why it's a problem**: WCAG 1.4.13 Content on Hover or Focus (AA) requires
  focus-triggerable, not just hover-triggerable, supplementary content.
- **Recommended change**: add `tabindex="0"` to each of the listed elements (copying
  `health-indicator`'s pattern), or move to `mat-icon-button` + `matTooltip` where the
  element should be a real control rather than decoration.
- **Cost**: S, 11 locations (7 in `coding-team-page.component.html`, 1 in `code-review-dashboard.component.html`, 3 in `out-of-scope-issues.component.html`), same one-line fix each.
- **Verification**: manual keyboard walkthrough — Tab to each listed element and
  confirm the tooltip opens.

### FLOW-2 — Coding Team's live-progress panel over-announces during polling
- **Severity**: Medium
- **Location**: `coding-team-monitor.component.html:2` (`role="status" aria-live="polite"` on the entire panel root)
- **What the user hits**: the whole monitor — objective text, progress %, phase
  stepper, per-agent roster with individual progress bars — sits inside one live
  region. Any single-field change during a poll tick (one agent's progress ticking a
  percent, an activity-detail string updating) risks re-announcing large swaths of this
  region's text to screen reader users repeatedly while a job runs.
- **Why it's a problem**: this works against the announcement's own purpose — a screen
  reader user gets spammed rather than informed. The codebase already solved this
  correctly two ways nearby: `coding-team-page.component.ts:369-402` uses small,
  debounced, purpose-built `visually-hidden aria-live="polite"` announcer divs instead
  of live-wrapping a whole subtree, and `shared/inline-banner` documents the same
  invariant explicitly in its own source comment.
- **Recommended change**: narrow the live region to one small sentence
  ("Status: {{ phase }}, {{ progressPercent }}% complete") built the same
  debounced-announcer way, and remove **both** `role="status"` and `aria-live="polite"` from
  the panel root — `role="status"` alone still implies a polite, atomic live region, so
  deleting only the explicit `aria-live` leaves the over-announcement in place. The
  replacement announcer element carries `aria-live="polite"` itself, matching the
  `coding-team-page.component.ts:369-402` pattern this finding cites.
- **Cost**: M (needs the debounce logic, not just a markup change), this component.
- **Verification**: `coding-team-monitor.component.a11y.spec.ts` — assert the live
  region's text changes at most once per meaningful state transition, not once per poll
  tick.

### CLARITY-2 — Hand-rolled loading/empty states lose the wiring the shared primitives already provide
- **Severity**: Medium
- **Location**: `coding-team-page.component.html:63-65,97-102,195-200` (hand-rolled `mat-spinner` + text, no `role="status"`); `out-of-scope-issues.component.html:17-21` (same)
- **What the user hits**: none of Coding Team's own loading spinners announce
  themselves to a screen reader — no `role="status"`/`aria-live` anywhere on them.
  `code-review-dashboard`, on the same team, gets this for free by reusing the shared
  `app-loading-spinner` (which internally sets `role="status" aria-live="polite"
  aria-busy="true"`) instead of hand-rolling the markup.
- **Why it's a problem**: WCAG 4.1.3 Status Messages (AA) for the missing announcement;
  Lens E (reuse existing primitives) for the root cause — this is "build a custom X
  when a shared X exists," and the custom version is measurably worse.
- **Recommended change**: replace the hand-rolled `<mat-spinner>…</mat-spinner>` blocks
  with `<app-loading-spinner [diameter]="24" message="…" />` at each listed location.
- **Cost**: S, four-plus locations, deletes code rather than adding it.
- **Verification**: extend each component's spec to assert the loading state renders
  via the shared component (a snapshot/selector check), not a bespoke `mat-spinner`.

### A11Y-8 — Systemic-findings chip's accessible name doesn't contain its visible label
- **Severity**: Low
- **Location**: `code-review-dashboard/pr-review-detail/pr-review-detail.component.html:95-102`
- **What the user hits**: the chip's visible text is `"{{ systemicCount }} systemic
  pattern(s)"` (line 101) but its `aria-label` is the fixed string `"View systemic
  findings"` (line 99), which fully overrides the accessible name and shares no words
  with the visible label. A voice-control user saying "click systemic pattern(s)" (what
  they can see) won't match the accessible name.
- **Why it's a problem**: WCAG 2.5.3 Label in Name (A) — this is a containment rule;
  the visible label's text must appear in the accessible name.
- **Recommended change**: drop the `aria-label` (the visible text is already clear on
  its own) or change it to contain the visible text, e.g.
  `aria-label="{{ systemicCount }} systemic pattern(s) — view details"`.
- **Cost**: S, one line.
- **Verification**: `pr-review-detail.component.a11y.spec.ts` already exists — add an
  accessible-name-contains-visible-text assertion for this chip.

### SYSTEM-1 — Empty-state icon opacity drags an already-tuned token below 3:1
- **Severity**: Low (the icons are `aria-hidden="true"` and sit directly beside text
  stating the same thing, so 1.4.11 Non-text Contrast's "pure decoration" exception
  plausibly applies — flagged for design-system consistency rather than asserted as a
  hard AA violation)
- **Location**: `user-interface/src/styles.scss:316-323` (`.kh-empty-icon`, shared —
  hits Coding Team's Jobs-empty and GitHub-unconfigured icons) and
  `software-engineering-dashboard.component.scss:24-31` (`.empty-state-icon`,
  SE-dashboard-specific)
- **What the user hits**: reproduced live via axe — the empty-state icon on SE
  dashboard/Coding-Team/Code-Review measures 1.85–1.93:1. Root cause: both rules apply
  `color: var(--kh-text-muted)` (`#909099`, ~6.6:1 against the `#000000` page
  background on its own — already AA-tuned) and then stack `opacity: 0.4` on top,
  which blends the rendered color toward the background and cuts the effective
  contrast to under 2:1.
- **Why it's a problem**: the token was already contrast-correct; the local `opacity`
  reduction reintroduces exactly the problem the token exists to prevent. Worth fixing
  even under the decorative exception, since a future reuse of `--kh-text-muted` at
  full opacity elsewhere shouldn't have to rediscover this.
- **Recommended change**: drop `opacity: 0.4` (the token alone reads clearly enough at
  48-64px), or reduce it much less aggressively (≥0.7 stays above 3:1).
- **Cost**: S, two locations (one shared, one SE-specific).
- **Verification**: real-browser contrast check on the icon, or simply remove the
  opacity and eyeball it against the surrounding `.kh-empty-message` text weight.

### CLARITY-3 — Eleven dead SE-flavored components should be deleted or fixed before reconnecting
- **Severity**: Low
- **Location**: see the Scoping correction section above for the full list and grep evidence.
- **What the user hits**: nobody, today — that's the point. Filed as a maintenance
  finding, not a user-facing one: these components are actively misleading to anyone
  auditing or extending "the SE pages" by directory listing (as this audit's own
  request initially did), and at least one (`product-analysis-run-form.component.scss:1,6`)
  carries hardcoded non-token colors that would need fixing before any reconnection.
- **Recommended change**: delete the eleven components, or if any are slated for reuse,
  track that intent somewhere discoverable (a tracking doc, not a code comment per this
  repo's own conventions) and fix their accessibility gaps before wiring them back into
  a route. Treat eleven as a floor rather than a census: this audit enumerated only the
  components it encountered while tracing the four in-scope routes, and a subsequent
  sweep of the same `run-team`-era family found further unreferenced directories beyond
  these eleven (`architecture-results`, `retry-failed`, and the `*-run-form` siblings of
  the listed `*-job-status` components among them). Re-run the selector-and-class-name
  grep over the whole `components/` directory before deleting, so the sweep is decided
  by evidence rather than by this list.
- **Cost**: S (delete) or M (fix-and-reconnect, per component).

## Positive findings (preserve these patterns)

Worth stating plainly so a future refactor doesn't accidentally "fix" what already
works:

- **`app-shell.component.ts:139-148`** moves focus to `#main-content` on every real
  route change (correctly skipping query-param-only updates and back/forward
  navigation) — a genuinely thorough SPA focus-management implementation.
- **`app-shell`'s nav-group flyout** (`app-shell.component.ts:181-301`) hand-implements the WAI-ARIA
  disclosure-navigation pattern completely and correctly: Enter/Space/ArrowRight opens
  and moves focus in, arrow keys rove, Home/End jump, Escape/ArrowLeft closes and
  restores focus to the trigger.
- **No div/span-with-click-and-no-keyboard-handler** exists anywhere in the four live
  SE routes' component tree — every custom clickable row is a real `<button>`. This is
  a very common failure pattern elsewhere and this codebase avoided it consistently.
- **`shared/inline-banner`** scopes its live region to the message only, deliberately
  excluding any projected interactive controls, with the rationale documented in its
  own source — the model every other live-region usage in this audit is judged against.
- **`coding-team-page.component.ts:369-402`** and **`code-review-dashboard.component.ts:138-151`**
  both implement small, debounced, purpose-built announcer live regions instead of
  live-wrapping a whole subtree — the correct pattern FLOW-2/A11Y-1's sibling
  `coding-team-monitor` should adopt.
- **`health-indicator`** triple-redundantly signals status via icon shape, color,
  *and* text/tooltip, and is the one place in this audit that correctly adds
  `tabindex="0"` to a `role="img"` specifically to make its tooltip keyboard-reachable
  — the pattern A11Y-7 asks the rest of the app to copy.
- **`MatDialog` usage** (transcript / systemic-findings dialogs) relies entirely on
  Angular Material's default focus trap + initial-focus + focus-restore behavior, with
  no custom overrides needed or missing — compliant by default.
- Reflow at 320 CSS px produces **no horizontal scrollbar** on any of the four SE
  routes (verified live) — though see "Out of scope, noted" below for what that check
  alone doesn't catch.

## Open questions

- **AAA target vs. AA bar**: this audit holds the AA line per house convention but
  the request that triggered it named AAA. Two known, self-documented AAA gaps exist
  that are *not* AA violations and are not filed as findings above:
  `--kh-warning` text/background pairs clear AA (≥4.5:1) but not AAA (7:1) per
  `theme.scss:36-37,100-101`'s own comment, and `out-of-scope-issues.component.scss`'s
  hardcoded severity-chip pairs measure ~4.9:1 (AA pass, AAA fail) for at least the
  `critical` pair. If AAA is a hard requirement, these need a product decision on
  whether to darken/lighten the palette (a token-level change, not a per-component
  one) — flagged here rather than as findings because AA-conformant color decisions
  are a design call, not a defect.
- **Should the eleven dead components (CLARITY-3) be deleted or revived?** Needs a product
  decision; this audit only surfaces that the ambiguity exists and its cost (stale
  lint surface, misleading directory listing).

## Out of scope, noted

- **App-shell's sidenav does not collapse at narrow viewports.** `mat-sidenav`'s
  `[mode]` is hardcoded to `'side'` with `[opened]="true"`
  (`app-shell.component.html:3-9`) and no `BreakpointObserver`/responsive logic exists
  anywhere in `app-shell.component.ts`. Reproduced live at a 320 CSS px viewport on all
  four SE routes: the sidenav still renders at its full ~185px width, squeezing the
  actual page content into a column so narrow that body text wraps to roughly one word
  per line (screenshot on file). This produces no horizontal scrollbar — so a
  scrollbar-only reflow check (like this audit's own automated pass) misses it — but it
  is a real WCAG 1.4.10 Reflow (AA) failure under the "loss of content/functionality"
  clause, on every single page in the app, not something SE's own templates created or
  can fix. Noted here per this review's own scope rule ("Global nav and theming as a
  whole" is explicitly out of scope) but flagged prominently given its severity and
  that the audit's own scope explicitly asked about mobile/tablet breakpoints.
- **`region` (moderate) axe finding on `.brand-text`/`.footer-profile-link`** — global
  app-shell chrome not contained by a landmark. Same "global nav as a whole" exclusion.
- **Global primary nav link has no visible focus indicator** — `app-shell`'s
  "Jobs Dashboard" link (`.nav-link-primary`) measured `outline: none, box-shadow: none`
  on focus in the live keyboard-tab-order check, a 2.4.7 Focus Visible failure — but
  it's global chrome outside SE's templates.
- **Material Icon ligatures rendered as raw text** (e.g. "co", "ch", "en") were
  observed in this session's screenshots when the Material Icons webfont failed to
  load. This reproduces in this sandboxed environment because outbound font requests
  are blocked, and a `connect-src` CSP violation was also logged for the same font
  URLs (`angular.json`'s dev-server CSP allows `fonts.googleapis.com` under
  `style-src` but not `connect-src`) — but I could not determine whether that CSP
  applies to the production build/deployment or is dev-server-only, so this is noted
  rather than filed as a finding. Worth a follow-up check against the real deployed CSP.

## Appendix

**Tools used**: Playwright (Chromium, pre-installed in this environment) driving a
locally-built `ng serve` instance of this branch; axe-core 4.x run against each route
in-browser (not jsdom, so `color-contrast` was active, unlike the repo's own unit-test
harness); manual source review of every live component's `.html`/`.ts`/`.scss`;
repo-wide `grep` census work per the house prompt's §2 (shared-primitive inventory,
`expectNoAxeViolations` coverage census, `--kh-*` token census, error-handler
interceptor read).

**States/pages I could exercise live vs. from source only**: the backend is not running
in this environment, so every route was tested live only in its unauthenticated/
API-unreachable state (which happens to exercise the API-error path directly and
usefully — see STATE-1). Populated/long-running/stalled states for Coding Team and
Code Review are evidenced from source (branch citations above), not from live
observation, since driving an actual job to completion requires the backend pipeline.
1.4.4 Resize Text (200% zoom) and 1.4.12 Text Spacing were not tested, for different
reasons. 1.4.4 needs an interactive pass — Playwright exposes no first-class browser-zoom
API — so it stays a manual follow-up. 1.4.12 is fully scriptable and simply was not run:
the WCAG-published text-spacing overrides can be injected with `page.addStyleTag(...)`
and the axe pass re-run, so that gap is automatable in a follow-up rather than
manual-only.

**No test account / role variable applies**: this app has no per-role (admin/member/
viewer) permission gating on the SE routes — confirmed by source review, not asserted
from absence of a login screen. Persona coverage for this audit is therefore keyboard,
screen-reader (via source/ARIA-tree review, not a live screen reader — none was
available in this environment), and low-vision/contrast users, not role-based.

**Existing automated coverage**: `expectNoAxeViolations` covers `code-review-dashboard`,
`pending-issue-proposals`, `pr-review-detail`, `coding-team-monitor`, and
`coding-team-page` (each via a `.a11y.spec.ts`). It does **not** cover `planning-page`,
`run-team-form`, `run-team-tracking`, `execution-tasks`, `execution-stream`,
`pending-questions`, `product-analysis-job-status`, `backend-code-v2-job-status`,
`frontend-code-v2-job-status`, `api-status-widget`, `start-from-spec-form`, or
`software-engineering-dashboard` — of these, `planning-page` and
`software-engineering-dashboard` are live routes with no axe coverage at all today.
