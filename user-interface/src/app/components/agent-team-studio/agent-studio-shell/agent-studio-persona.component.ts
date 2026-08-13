import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { MatButtonModule } from '@angular/material/button';
import { MatDialog, MatDialogModule } from '@angular/material/dialog';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatTooltipModule } from '@angular/material/tooltip';
import { Router } from '@angular/router';
import { EMPTY, Subscription, catchError, interval, switchMap, timeout } from 'rxjs';
import { AgenticTeamApiService } from '../../../services/agentic-team-api.service';
import { PersonaTestingApiService } from '../../../services/persona-testing-api.service';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import type {
  AgenticTeam,
  ProcessDefinition,
  TestPipelineRun,
} from '../../../models/agentic-team.model';
import type { PersonaInfo, PersonaTestRunDetail } from '../../../models/persona-testing.model';
import { AgenticTeamTestPanelComponent } from '../agentic-team-test-panel/agentic-team-test-panel.component';
import {
  PersonaEditorDialogComponent,
  type PersonaEditorDialogData,
  type PersonaEditorDialogResult,
} from '../persona-testing-dashboard/persona-editor-dialog.component';

/**
 * Persona-test run statuses that are terminal (polling stops). Both the British
 * ('cancelled') and American ('canceled') spellings are accepted because the
 * backend pipeline status string is not normalized at the source.
 */
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled', 'canceled']);
/** Live-run poll cadence (ms). Matches the founder run's coarse 15–30s ticks. */
const POLL_MS = 10_000;
/** Upper bound on the launch (`POST /start`) request so a hung call can't wedge the UI. */
const LAUNCH_TIMEOUT_MS = 30_000;
/** Transient banner shown on a failed poll; cleared on the next successful poll. */
const LOST_CONTACT = 'Lost contact with the run; retrying…';
/**
 * The pipeline `step_results[].status` value marking a fully-finished step (an
 * answered WAIT step also lands here). Named so the coupling to the backend's
 * step-status vocabulary (`PipelineStepResult.status`, an untyped string) is
 * explicit and greppable rather than a bare literal in a filter.
 */
const STEP_STATUS_COMPLETED = 'completed';
/** Width of the persona create/edit dialog; matches the dashboard's editor dialog. */
const PERSONA_DIALOG_WIDTH = '560px';

/**
 * Human-readable labels for the founder run's internal status strings, so the UI
 * never shows raw wire values (`polling_build`, `answering_build_questions`) to
 * users or reads them out to a screen reader. Unmapped values fall back to a
 * prettified form (see {@link humanizeStatus}).
 */
const STATUS_LABELS: Record<string, string> = {
  pending: 'Starting…',
  generating_spec: 'Preparing…',
  submitting_analysis: 'Analyzing…',
  polling_analysis: 'Analyzing…',
  answering_analysis_questions: 'Answering a question…',
  submitting_build: 'Running…',
  polling_build: 'Running…',
  answering_build_questions: 'Answering a question…',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
  canceled: 'Cancelled',
};

/** Map a raw run status to a user-facing label, prettifying anything unmapped. */
function humanizeStatus(status: string): string {
  if (!status) {
    return 'Unknown';
  }
  return (
    STATUS_LABELS[status] ??
    status.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
  );
}

// Back-loop destinations as 0-based indices into STUDIO_STAGES
// (build=0, test=1, compose=2, personas=3). Named so the back-loops don't carry
// bare magic numbers; keep in sync with the STUDIO_STAGES order.
const STAGE_TEST_AGENT = 1;
const STAGE_COMPOSE_TEAM = 2;

type StudioPersonaMode = 'manual' | 'persona';

/**
 * Agent Studio — Stage 4 "Test Team with Personas" (spec §3, Stage 4).
 *
 * Validates the team assembled in Stage 3 two ways:
 *   - **Manual:** reuses `app-agentic-team-test-panel` (chat + pipeline) as-is.
 *   - **Persona-driven:** picks a testing persona + a target process and launches
 *     an autonomous run via `POST /start` with
 *     `target_team_key = "agentic_team:<teamId>"`, then renders a live run view
 *     (elapsed counter, "persona is thinking…", decision transcript).
 *
 * Back-loops (spec §2.1): "iterate roster" → Stage 3, "fix an agent" → Stage 2
 * (disabled when no registry agent is in focus). A team that isn't testable yet
 * (no `complete` process) shows the §Stage-3 safety net rather than an empty
 * dropdown. Reads the handoff `teamId`/`processId`; never navigates backward via
 * the stepper itself.
 */
@Component({
  selector: 'app-agent-studio-persona',
  standalone: true,
  imports: [
    MatButtonModule,
    MatIconModule,
    MatProgressBarModule,
    MatTooltipModule,
    MatDialogModule,
    AgenticTeamTestPanelComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-persona.component.html',
  styleUrl: './agent-studio-persona.component.scss',
})
export class AgentStudioPersonaComponent implements OnInit {
  private readonly state = inject(AgentStudioStateService);
  private readonly agenticApi = inject(AgenticTeamApiService);
  private readonly personaApi = inject(PersonaTestingApiService);
  private readonly dialog = inject(MatDialog);
  private readonly destroyRef = inject(DestroyRef);
  private readonly router = inject(Router);

  readonly mode = signal<StudioPersonaMode>('persona');

  readonly team = signal<AgenticTeam | null>(null);
  readonly teamError = signal<string | null>(null);
  readonly personas = signal<PersonaInfo[]>([]);
  /** True while the persona library is being fetched (distinguishes "loading" from "empty"). */
  readonly personasLoading = signal(false);
  /** Persona-library load failure, owned separately from the run/launch `error`. */
  readonly personasError = signal<string | null>(null);
  /** True while a create-persona POST is in flight (drives a progress indicator). */
  readonly creatingPersona = signal(false);
  readonly selectedProcessId = signal<string | null>(null);
  readonly launching = signal(false);
  /** True while a stop-run (cancel) request is in flight; drives "Stopping…". */
  readonly cancelling = signal(false);
  readonly error = signal<string | null>(null);

  // ── Live run ────────────────────────────────────────────────────────────
  readonly run = signal<PersonaTestRunDetail | null>(null);
  readonly elapsedSec = signal(0);
  /**
   * The underlying agentic test-pipeline run, read directly from the
   * provisioning service so the header can show real step/WAIT progress the
   * founder `/status` endpoint collapses away. Populated once the founder run
   * carries an `se_job_id` (which, for `agentic_team:*` targets, IS the pipeline
   * run id — see the orchestrator's build-phase `_on_started`). Null until then,
   * and not refreshed once the run is terminal (the progress UI is hidden then,
   * so a terminal read would be wasted).
   */
  readonly pipelineRun = signal<TestPipelineRun | null>(null);
  private pollSub: Subscription | null = null;
  private elapsedSub: Subscription | null = null;
  /**
   * The run currently being polled. Guards against a stale in-flight status
   * response (e.g. the one-shot immediate fetch) from a previous run landing
   * after a new run started and clobbering it / stopping the new poller.
   */
  private activeRunId: string | null = null;
  /** Guards `newPersona` against opening multiple editor dialogs on rapid clicks. */
  private dialogOpen = false;

  readonly teamId = computed(() => this.state.teamId());
  readonly selectedPersonaId = computed(() => this.state.personaId());

  /** Only `complete` processes can be driven end-to-end (spec Stage 3 gate). */
  readonly completeProcesses = computed<ProcessDefinition[]>(() =>
    (this.team()?.processes ?? []).filter((p) => p.status === 'complete'),
  );

  /**
   * The Stage-4 safety net: a loaded team with no `complete` process can't be
   * tested. This is decided from the **locally loaded** team — the authoritative
   * source for process status — rather than the founder service's
   * `/testable-teams` list, which best-effort-omits agentic teams on a
   * cross-service enumeration outage and would otherwise false-block a team whose
   * complete processes are right here.
   */
  readonly noCompleteProcess = computed(
    () => !!this.team() && this.completeProcesses().length === 0,
  );

  /** A registry agent must be in focus to "fix an agent" in the sandbox (Stage 2). */
  readonly canFixAgent = computed(() => !!this.state.registryAgentId());

  /**
   * True while the team fetch is in flight: no team yet and no load error. Drives
   * a "Loading team…" indicator in persona mode (matching manual mode) so the
   * launcher isn't shown with an empty process dropdown before data arrives.
   */
  readonly teamLoading = computed(() => !this.team() && !this.teamError());

  readonly runTerminal = computed(() => {
    const r = this.run();
    return r ? TERMINAL_STATUSES.has(r.status) : false;
  });

  /**
   * The underlying pipeline run has itself reached a terminal state
   * (completed/failed/cancelled). The founder `/status` the run head shows lags
   * the pipeline by up to a poll interval, so a pipeline that has already
   * failed/finished can still be reported as in-progress by the founder run for
   * ~10s. Keying "live" off the pipeline's own status too means a dead run stops
   * rendering the in-progress bar + "thinking…" promptly instead of masquerading
   * as healthy during that lag.
   */
  readonly pipelineTerminal = computed(() => {
    const s = this.pipelineRun()?.status;
    return s ? TERMINAL_STATUSES.has(s) : false;
  });

  /**
   * The **founder job** is still active (a run exists and the founder status is
   * non-terminal), regardless of the pipeline's state. Gates the *job* controls —
   * Stop, and the disabled launcher — because the founder run is cancellable and
   * must not be superseded until it terminates, even during the ~poll-interval
   * window where the pipeline already ended but the founder status hasn't caught
   * up. (Contrast `runLive`, which also requires the pipeline to be live and gates
   * the *progress* display.) Requires `run()` so it is false before any launch.
   */
  readonly runInProgress = computed(() => this.run() != null && !this.runTerminal());

  /**
   * There is live *work to display*: a run exists and neither the founder run nor
   * the pipeline is terminal. Gates the progress bar + "thinking…" cue, so a
   * finished/failed pipeline stops rendering as healthy in-progress even while the
   * founder status lags (see `pipelineTerminal`).
   */
  readonly runLive = computed(
    () => this.run() != null && !this.runTerminal() && !this.pipelineTerminal(),
  );

  // ── Live-run progress (spec §Stage 4 "Run progress UI") ───────────────────
  // The header sets expectations for slow autonomous runs: an elapsed counter
  // (above), an animated "persona is thinking…" indicator, and a step progress
  // bar when the process DAG length is known — falling back to an indeterminate
  // bar otherwise. Step/WAIT data comes from `pipelineRun` (the real pipeline
  // run), not the founder `/status` payload, which omits it.

  /**
   * The process being driven. Prefers the **live run's own** `process_id` and
   * only falls back to the launcher selection before a run exists — the launcher
   * `<select>` stays interactive during a run, so keying off it would let a
   * mid-run dropdown change desync the "step N of M" denominator and step-name
   * lookup from the numerator (which comes from `pipelineRun.step_results`).
   */
  readonly selectedProcess = computed<ProcessDefinition | undefined>(() => {
    const procId = this.pipelineRun()?.process_id ?? this.selectedProcessId();
    return (this.team()?.processes ?? []).find((p) => p.process_id === procId);
  });

  /**
   * DAG length (the "of M" denominator). For a branching DAG this is an upper
   * bound — a run executes one path — so "step N of M" is an accepted
   * approximation (the spec gates the bar only on "DAG length known").
   */
  readonly totalSteps = computed(() => this.selectedProcess()?.steps?.length ?? 0);

  /**
   * Count of steps the pipeline has actually *finished* (status `completed`,
   * which an answered WAIT step also reaches), excluding the one currently
   * running/waiting. This is the honest, backend-aligned progress unit: the
   * runner records an action/decision step's result only *after* it finishes,
   * so `step_results.length` is unreliable as "current step" (it lags the
   * running action by one but includes an in-flight WAIT). `completed`-count
   * sidesteps that — it is the numerator for both the bar and the step number.
   */
  readonly completedStepCount = computed(
    () =>
      this.pipelineRun()?.step_results?.filter((r) => r.status === STEP_STATUS_COMPLETED).length ??
      0,
  );

  /**
   * The step currently being worked on (1-based), for the "step N of M" label.
   * Derived from the SAME source as `currentStepName` — the pipeline cursor
   * `current_step_id` — so the number and the name can never disagree: it is the
   * cursor step's position among the recorded steps (its `step_results` index +
   * 1), or, when the cursor points at a not-yet-recorded running step, one past
   * the recorded ones. This matters at a step boundary: the runner records a
   * step 'completed' and advances the cursor in two separate writes, so a poll
   * landing between them sees the cursor still on the just-finished step —
   * deriving the number from that cursor keeps the label consistent (e.g.
   * "step 2 · <s2 name>", never "step 3 · <s2 name>"). Clamped to the DAG length
   * so a looped/branching run can never read a nonsensical "step 5 of 4".
   */
  readonly currentStepNumber = computed(() => {
    const pr = this.pipelineRun();
    if (!pr) {
      return 0;
    }
    const results = pr.step_results ?? [];
    const cursor = pr.current_step_id;
    const idx = cursor ? results.findIndex((r) => r.step_id === cursor) : -1;
    const position = idx >= 0 ? idx + 1 : results.length + 1;
    return Math.min(position, this.totalSteps());
  });

  /**
   * True once "step N of M" can be shown: a live run with a known DAG length.
   * (The bar itself switches determinate/indeterminate on whether any step has
   * finished yet — see the template — so a just-started run shows the moving
   * indeterminate bar rather than a determinate bar frozen at 0%.)
   */
  readonly stepProgressKnown = computed(
    () => this.runLive() && this.totalSteps() > 0 && this.pipelineRun() != null,
  );

  /**
   * Determinate bar value = fraction of steps *completed*, not started, so it
   * never pins at 100% while the final step is still executing/waiting (which
   * would read as "finished" on a run that hasn't finished). Clamped against a
   * branching/looped over-count.
   */
  readonly stepPercent = computed(() => {
    const total = this.totalSteps();
    if (total <= 0) {
      return 0;
    }
    // Floor (not round) so the bar can't display 100% while the final step is
    // still running — round() would hit 100 at completed/total ≥ 0.995 (e.g. a
    // hypothetical 199-of-200), contradicting the "never pins at 100%" contract.
    return Math.min(100, Math.floor((this.completedStepCount() / total) * 100));
  });

  /** Name of the current pipeline step, for a "step 2 of 4 · Write" label. */
  readonly currentStepName = computed(() => {
    const stepId = this.pipelineRun()?.current_step_id;
    if (!stepId) {
      return '';
    }
    return this.selectedProcess()?.steps?.find((s) => s.step_id === stepId)?.name ?? '';
  });

  /** The pipeline paused on a free-text WAIT step: the persona is formulating an answer. */
  readonly isWaiting = computed(() => this.pipelineRun()?.status === 'waiting_for_input');

  /** User-facing label for the current run status (never the raw wire value). */
  statusLabel(status: string): string {
    return humanizeStatus(status);
  }

  /**
   * A screen-reader-only sentence announced on meaningful run-state transitions
   * (started / answering / completed / failed / cancelled), so an assistive-tech
   * user driving an unattended run hears state changes without polling the DOM.
   * Kept to run-level transitions — the per-step "step N of M" live region owns
   * step progress — and, because `aria-live` only speaks on text *change*, a
   * status that holds steady across polls is not re-announced.
   */
  readonly runAnnouncement = computed(() => {
    const r = this.run();
    if (!r) {
      return '';
    }
    if (this.isWaiting()) {
      return 'The persona is answering a question.';
    }
    switch (r.status) {
      case 'completed':
        return 'Persona test completed.';
      case 'failed':
        return 'Persona test failed.';
      case 'cancelled':
      case 'canceled':
        return 'Persona test cancelled.';
      default:
        return 'Persona test running.';
    }
  });

  ngOnInit(): void {
    const teamId = this.teamId();
    if (!teamId) {
      return; // empty state: no team composed yet (handled in template)
    }
    // Pre-seed the target process from the Stage-3 handoff *before* loading the
    // team, so loadTeam's "default to the single complete process" only fires
    // when the handoff carried none (it must not clobber a seeded selection).
    this.selectedProcessId.set(this.state.processId());
    this.loadTeam(teamId);
    this.loadPersonas();
  }

  /** Switch the Stage-4 sub-mode between 'manual' (chat/pipeline) and 'persona'. */
  setMode(mode: StudioPersonaMode): void {
    this.mode.set(mode);
  }

  /**
   * ARIA tab-pattern keyboard navigation for the sub-mode tablist: Left/Right
   * move to the adjacent tab, Home/End to the first/last. Activates the target
   * tab (this is an automatic-activation tablist) and moves focus to it, which —
   * combined with the roving `tabindex` in the template (only the active tab is
   * `tabindex=0`) — satisfies the APG tab keyboard contract.
   */
  onTabKeydown(event: KeyboardEvent): void {
    const navKeys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
    if (!navKeys.includes(event.key)) {
      return;
    }
    event.preventDefault();
    // Two tabs: Left/Right toggle, Home→manual, End→persona.
    const target: StudioPersonaMode =
      event.key === 'Home'
        ? 'manual'
        : event.key === 'End'
          ? 'persona'
          : this.mode() === 'manual'
            ? 'persona'
            : 'manual';
    this.setMode(target);
    // currentTarget is the focused tab button; its tablist parent owns both tabs.
    const list = (event.currentTarget as HTMLElement).closest('[role="tablist"]');
    list?.querySelector<HTMLElement>(`#studio-tab-${target}`)?.focus();
  }

  /**
   * Select the testing persona to drive the run. Persona selection is owned by
   * the shared studio state (so it survives a back-loop to Stage 2/3), hence the
   * write goes through the state service rather than a local signal.
   */
  selectPersona(id: string): void {
    this.state.setPersonaId(id);
  }

  /** Select the target process for the run (must be a `complete` process). */
  selectProcess(id: string): void {
    this.selectedProcessId.set(id);
  }

  // ── Data loads ────────────────────────────────────────────────────────────

  private loadTeam(teamId: string): void {
    this.teamError.set(null);
    this.agenticApi
      .getTeam(teamId)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (resp) => {
          // A 200 with no team (e.g. the id resolved to nothing, or the HTTP
          // client mapped an empty body to null) must surface as an error, not
          // leave teamLoading() stuck true forever on "Loading team…".
          if (!resp || !resp.team) {
            this.teamError.set('Team not found.');
            return;
          }
          this.team.set(resp.team);
          // Signals are synchronous, so `completeProcesses` (derived from
          // `team`) is already up to date here — read it instead of
          // re-filtering `resp.team.processes` a second time.
          const complete = this.completeProcesses();
          const current = this.selectedProcessId();
          // Drop a handoff-seeded selection that isn't a *complete* process: the
          // <select> only lists complete ones (so it'd show the placeholder) and
          // the backend would 422 it, but the signal would still enable Run.
          if (current && !complete.some((p) => p.process_id === current)) {
            this.selectedProcessId.set(null);
          }
          // Default to the only complete process when nothing valid is selected.
          if (!this.selectedProcessId() && complete.length === 1) {
            this.selectedProcessId.set(complete[0].process_id);
          }
        },
        error: () => {
          this.teamError.set('Could not load this team.');
        },
      });
  }

  private loadPersonas(): void {
    // Owns its own `personasError` (cleared here, set on failure) so the library
    // area can show a load error without touching the run/launch `error` signal —
    // each error region is independently responsible and safely reloadable.
    this.personasError.set(null);
    this.personasLoading.set(true);
    this.personaApi
      .getPersonas()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (resp) => {
          this.personasLoading.set(false);
          // `resp?.` guards a null body (empty 200 / network-mapped null) so the
          // library degrades to an empty list rather than throwing.
          const personas = resp?.personas ?? [];
          this.personas.set(personas);
          // Default the persona selection when none carried from the handoff, OR
          // when the handoff-seeded id isn't in the loaded list (e.g. it was
          // deleted) — otherwise nothing would be highlighted and Run could be
          // enabled for a persona that no longer exists.
          const current = this.selectedPersonaId();
          if ((!current || !personas.some((p) => p.id === current)) && personas.length > 0) {
            this.state.setPersonaId(personas[0].id);
          }
        },
        error: () => {
          this.personasLoading.set(false);
          this.personasError.set('Could not load personas.');
        },
      });
  }

  // ── Launch + live run ───────────────────────────────────────────────────

  /**
   * Start an autonomous persona-driven run against the selected complete process
   * via `POST /start` (`target_team_key = "agentic_team:<teamId>"`), then begin
   * polling its status. No-ops unless a persona, a process, and a team are all
   * selected and no launch is already in flight (the button is disabled in the
   * same conditions; this guard makes the precondition explicit for direct calls).
   */
  launch(): void {
    const teamId = this.teamId();
    const personaId = this.selectedPersonaId();
    const processId = this.selectedProcessId();
    // Also require the team to have loaded: a programmatic call (or a stale
    // handoff-seeded processId after a failed load) must not fire a request that
    // would certainly fail. (Superseding a live run is prevented at the UI — the
    // launcher is disabled while `runInProgress()` — not here, so tests can still
    // drive status transitions through launch().)
    if (!teamId || !personaId || !processId || this.launching() || !this.team()) {
      return;
    }
    this.launching.set(true);
    this.error.set(null);
    this.personaApi
      .startTest({
        persona_id: personaId,
        target_team_key: `agentic_team:${teamId}`,
        process_id: processId,
      })
      // Bound the request so a hung connection can't leave `launching` stuck true
      // with no feedback; the error branch surfaces a banner and re-enables Run.
      .pipe(timeout(LAUNCH_TIMEOUT_MS), takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (resp) => {
          this.launching.set(false);
          // A null/jobless response can't be polled; surface an error instead of
          // calling startPolling(undefined) and silently stalling the run view.
          if (!resp || !resp.job_id) {
            this.error.set('Could not start the persona test.');
            return;
          }
          this.startPolling(resp.job_id);
        },
        error: () => {
          this.launching.set(false);
          this.error.set('Could not start the persona test.');
        },
      });
  }

  /**
   * Open the full audit view for the current persona run inside Studio.
   *
   * Preconditions: none (safe to call with no run).
   * Postconditions: when `run()` is set, navigates to
   *   `/agent-studio/persona-run/:runId` with that run's `run_id`. When `run()`
   *   is null, does not navigate.
   */
  openFullAudit(): void {
    const id = this.run()?.run_id;
    if (!id) return;
    void this.router.navigate(['/agent-studio', 'persona-run', id]);
  }

  /**
   * Cancel the in-flight founder run via the cancel endpoint. No-ops unless the
   * founder job is in progress and no stop is already pending.
   *
   * The cancel endpoint marks the founder run terminal ("failed", "Cancelled by
   * user") synchronously, so `cancelling` is intentionally kept **true on
   * success**: the Stop button stays disabled ("Stopping…") until the run's next
   * poll flips `runInProgress()` false and hides the control (≤ one poll). That
   * prevents a re-click firing a *redundant* cancel that 409s and banners a
   * spurious error over a run that was in fact cancelled. Only a genuine failure
   * or a timed-out request re-enables Stop for a retry.
   *
   * The error handler is scoped to the run that was stopped (`run_id` guard): a
   * late failure from a *superseded* run's cancel (e.g. a 404/409 after the user
   * launched a new run) must not banner or reset the current run. `timeout` bounds
   * a hung request so it can't leave the button stuck disabled with no response.
   */
  stopRun(): void {
    const r = this.run();
    if (!r || !this.runInProgress() || this.cancelling()) {
      return;
    }
    const runId = r.run_id;
    this.cancelling.set(true);
    this.error.set(null);
    this.personaApi
      .cancelJob(runId)
      .pipe(timeout(LAUNCH_TIMEOUT_MS), takeUntilDestroyed(this.destroyRef))
      .subscribe({
        error: () => {
          if (this.run()?.run_id === runId) {
            this.cancelling.set(false);
            this.error.set('Could not stop the run.');
          }
        },
      });
  }

  private startPolling(runId: string): void {
    this.stopPolling();
    this.activeRunId = runId;
    this.run.set(null);
    // Clear the prior run's pipeline state so a new launch doesn't briefly show
    // the last run's step progress before the first pipeline read lands.
    this.pipelineRun.set(null);
    // A fresh run can't be mid-stop; clear a stale "Stopping…" from a prior run.
    this.cancelling.set(false);
    this.elapsedSec.set(0);
    this.elapsedSub = interval(1000)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(() => {
        if (!this.runTerminal()) {
          this.elapsedSec.update((s) => s + 1);
        }
      });
    this.pollSub = interval(POLL_MS)
      .pipe(
        // Handle the failure INSIDE switchMap: a transient getRunStatus error
        // must not propagate to the outer interval subscription (that would
        // terminate the stream permanently). catchError → EMPTY surfaces a
        // banner and lets the next tick retry, matching the immediate-fetch
        // comment's promise.
        switchMap(() =>
          this.personaApi.getRunStatus(runId).pipe(
            catchError(() => {
              this.error.set(LOST_CONTACT);
              return EMPTY;
            }),
          ),
        ),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe((detail) => this.handleStatus(detail));
    // Fetch once immediately so the panel isn't blank for a full poll interval.
    this.personaApi
      .getRunStatus(runId)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (detail) => this.handleStatus(detail),
        error: () => {
          // Guard against a stale fetch landing after a newer run was launched:
          // only banner the run that is still active. Use the LOST_CONTACT
          // constant so handleStatus clears it on the next good poll (it matches
          // by value). The interval poll will retry meanwhile.
          if (this.activeRunId === runId) {
            this.error.set(LOST_CONTACT);
          }
        },
      });
  }

  /** Apply a polled status; stop polling once the run reaches a terminal state. */
  private handleStatus(detail: PersonaTestRunDetail): void {
    // Ignore a null/undefined payload (defensive against a malformed response
    // slipping past the error handler) and a stale response from a superseded
    // run (e.g. the previous run's in-flight immediate fetch) so neither can
    // clobber the current run or stop its poller.
    if (!detail || detail.run_id !== this.activeRunId) {
      return;
    }
    // Clear ONLY the transient "lost contact" banner on a successful poll —
    // not unrelated load/create errors (which own the same signal).
    if (this.error() === LOST_CONTACT) {
      this.error.set(null);
    }
    this.run.set(detail);
    const terminal = TERMINAL_STATUSES.has(detail.status);
    // Piggyback a pipeline-run read on the founder poll (same 10s cadence, no
    // second poller to manage) to refresh the real step/WAIT progress. Skipped
    // once terminal: the progress UI is hidden then, so the read would be wasted.
    const teamId = this.teamId();
    if (detail.se_job_id && teamId && !terminal) {
      this.fetchPipelineRun(teamId, detail.se_job_id);
    }
    if (terminal) {
      this.stopPolling();
    }
  }

  /**
   * Read the underlying agentic test-pipeline run for its step/WAIT progress.
   * Best-effort and self-contained: a failure (e.g. a non-agentic `se_job_id`
   * with no pipeline run, or a transient outage) is swallowed and the header
   * simply degrades to the indeterminate "thinking…" bar — it never surfaces an
   * error or throws. Guarded against a superseded run by matching the response's
   * `run_id` to the current run's `se_job_id` (a newer run has a different one),
   * so a slow read from a prior run can't clobber the current one.
   */
  private fetchPipelineRun(teamId: string, pipelineRunId: string): void {
    this.agenticApi
      .getPipelineRun(teamId, pipelineRunId)
      .pipe(
        catchError(() => EMPTY),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe((pr) => {
        if (pr && this.run()?.se_job_id === pr.run_id) {
          this.pipelineRun.set(pr);
        }
      });
  }

  private stopPolling(): void {
    this.pollSub?.unsubscribe();
    this.elapsedSub?.unsubscribe();
    this.pollSub = null;
    this.elapsedSub = null;
    // Clear the active run id so a late status callback for the just-stopped run
    // is guarded out by handleStatus; startPolling re-sets it for the next run.
    this.activeRunId = null;
  }

  // ── Persona authoring ─────────────────────────────────────────────────────

  /**
   * Open the persona editor dialog in 'create' mode; on a non-null result, POST
   * the new persona, append it to the library, and select it. The dialog is
   * closed on component destroy so it can't be orphaned in the overlay.
   */
  newPersona(): void {
    // Guard rapid double-clicks so we don't stack multiple editor dialogs in the
    // overlay, and don't re-open while a previous create is still in flight.
    if (this.dialogOpen || this.creatingPersona()) {
      return;
    }
    // dialog.open is synchronous, so set the guard only *after* it succeeds — if
    // it throws, the flag stays false and a later click can retry (a thrown open
    // must not permanently block persona creation).
    let ref;
    try {
      ref = this.dialog.open<
        PersonaEditorDialogComponent,
        PersonaEditorDialogData,
        PersonaEditorDialogResult
      >(PersonaEditorDialogComponent, { data: { mode: 'create' }, width: PERSONA_DIALOG_WIDTH });
    } catch {
      this.error.set('Could not open the persona editor.');
      return;
    }
    this.dialogOpen = true;
    // Close the dialog if the component is destroyed (e.g. the stepper moves to
    // another stage) so it isn't orphaned in the overlay. Capture the cleanup
    // function and call it once the dialog closes normally — otherwise every
    // open/close cycle over the component's lifetime registers one more
    // onDestroy callback that's never removed.
    const removeOnDestroy = this.destroyRef.onDestroy(() => ref.close());
    ref
      .afterClosed()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((result) => {
        removeOnDestroy();
        this.dialogOpen = false;
        if (!result) {
          return;
        }
        // Flag the in-flight create so the UI can show progress and disable the
        // "New persona" trigger — without this the dialog is already closed and a
        // user with no feedback might retry and double-submit.
        this.creatingPersona.set(true);
        this.personaApi
          .createPersona(result)
          .pipe(takeUntilDestroyed(this.destroyRef))
          .subscribe({
            next: (created) => {
              this.creatingPersona.set(false);
              // Guard a null/idless body so we don't push a bogus entry into the
              // library or select a persona with an undefined id.
              if (!created || !created.id) {
                this.error.set('Could not create the persona.');
                return;
              }
              this.personas.update((list) => [...list, created]);
              this.state.setPersonaId(created.id);
            },
            error: () => {
              this.creatingPersona.set(false);
              // Shared run-config `error` signal (intentionally — launch and
              // create are both run-config-area actions, never surfaced at once,
              // and launch() clears it before starting). The personas-*library*
              // load error has its own `personasError` signal.
              this.error.set('Could not create the persona.');
            },
          });
      });
  }

  // ── Back-loops (programmatic; the stepper stays forward-only) ─────────────

  /** Back-loop to Stage 3 (Compose Team) to revise the roster. */
  iterateRoster(): void {
    this.state.navigateToStage(STAGE_COMPOSE_TEAM);
  }

  /**
   * Back-loop to Stage 2 (Test Agent) to fix the in-focus registry agent. No-ops
   * when no registry agent is in focus (`canFixAgent()` is false), matching the
   * disabled toolbar button.
   */
  fixAgent(): void {
    if (this.canFixAgent()) {
      this.state.navigateToStage(STAGE_TEST_AGENT);
    }
  }

  /** Jump to Stage 3 (Compose Team) from an empty/safety-net state. */
  finishInCompose(): void {
    this.state.navigateToStage(STAGE_COMPOSE_TEAM);
  }
}
