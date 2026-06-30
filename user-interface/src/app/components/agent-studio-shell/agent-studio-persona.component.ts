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
import { MatTooltipModule } from '@angular/material/tooltip';
import { EMPTY, Subscription, catchError, interval, switchMap, timeout } from 'rxjs';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import { PersonaTestingApiService } from '../../services/persona-testing-api.service';
import { AgentStudioStateService } from '../../services/agent-studio-state.service';
import type { AgenticTeam, ProcessDefinition } from '../../models/agentic-team.model';
import type { PersonaInfo, PersonaTestRunDetail } from '../../models/persona-testing.model';
import { AgenticTeamTestPanelComponent } from '../agentic-team-test-panel/agentic-team-test-panel.component';
import {
  PersonaEditorDialogComponent,
  type PersonaEditorDialogData,
  type PersonaEditorDialogResult,
} from '../persona-testing-dashboard/persona-editor-dialog.component';

/** Persona-test run statuses that are terminal (polling stops). */
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled']);
/** Live-run poll cadence (ms). Matches the founder run's coarse 15–30s ticks. */
const POLL_MS = 10_000;
/** Upper bound on the launch (`POST /start`) request so a hung call can't wedge the UI. */
const LAUNCH_TIMEOUT_MS = 30_000;
/** Transient banner shown on a failed poll; cleared on the next successful poll. */
const LOST_CONTACT = 'Lost contact with the run; retrying…';

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

  readonly mode = signal<StudioPersonaMode>('persona');

  readonly team = signal<AgenticTeam | null>(null);
  readonly teamError = signal<string | null>(null);
  readonly personas = signal<PersonaInfo[]>([]);
  /** True while the persona library is being fetched (distinguishes "loading" from "empty"). */
  readonly personasLoading = signal(false);
  /** Persona-library load failure, owned separately from the run/launch `error`. */
  readonly personasError = signal<string | null>(null);
  readonly selectedProcessId = signal<string | null>(null);
  readonly launching = signal(false);
  readonly error = signal<string | null>(null);

  // ── Live run ────────────────────────────────────────────────────────────
  readonly run = signal<PersonaTestRunDetail | null>(null);
  readonly elapsedSec = signal(0);
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
          // A 200 with no team (e.g. the id resolved to nothing) must surface as
          // an error, not leave teamLoading() stuck true forever on "Loading team…".
          if (!resp.team) {
            this.teamError.set('Team not found.');
            return;
          }
          this.team.set(resp.team);
          const complete = resp.team.processes.filter((p) => p.status === 'complete');
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
          const personas = resp.personas ?? [];
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
    // would certainly fail.
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
          this.startPolling(resp.job_id);
        },
        error: () => {
          this.launching.set(false);
          this.error.set('Could not start the persona test.');
        },
      });
  }

  private startPolling(runId: string): void {
    this.stopPolling();
    this.activeRunId = runId;
    this.run.set(null);
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
    // Ignore a stale response from a superseded run (e.g. the previous run's
    // in-flight immediate fetch) so it can't clobber the current run or stop its
    // poller.
    if (detail.run_id !== this.activeRunId) {
      return;
    }
    // Clear ONLY the transient "lost contact" banner on a successful poll —
    // not unrelated load/create errors (which own the same signal).
    if (this.error() === LOST_CONTACT) {
      this.error.set(null);
    }
    this.run.set(detail);
    if (TERMINAL_STATUSES.has(detail.status)) {
      this.stopPolling();
    }
  }

  private stopPolling(): void {
    this.pollSub?.unsubscribe();
    this.elapsedSub?.unsubscribe();
    this.pollSub = null;
    this.elapsedSub = null;
  }

  // ── Persona authoring ─────────────────────────────────────────────────────

  /**
   * Open the persona editor dialog in 'create' mode; on a non-null result, POST
   * the new persona, append it to the library, and select it. The dialog is
   * closed on component destroy so it can't be orphaned in the overlay.
   */
  newPersona(): void {
    // Guard rapid double-clicks so we don't stack multiple editor dialogs in the
    // overlay; reset once this one closes.
    if (this.dialogOpen) {
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
      >(PersonaEditorDialogComponent, { data: { mode: 'create' }, width: '560px' });
    } catch {
      this.error.set('Could not open the persona editor.');
      return;
    }
    this.dialogOpen = true;
    // Close the dialog if the component is destroyed (e.g. the stepper moves to
    // another stage) so it isn't orphaned in the overlay.
    this.destroyRef.onDestroy(() => ref.close());
    ref
      .afterClosed()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((result) => {
        this.dialogOpen = false;
        if (!result) {
          return;
        }
        this.personaApi
          .createPersona(result)
          .pipe(takeUntilDestroyed(this.destroyRef))
          .subscribe({
            next: (created) => {
              this.personas.update((list) => [...list, created]);
              this.state.setPersonaId(created.id);
            },
            error: () => {
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
