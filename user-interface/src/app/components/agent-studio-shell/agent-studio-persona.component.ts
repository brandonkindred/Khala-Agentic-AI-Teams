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
import { EMPTY, Subscription, catchError, interval, switchMap } from 'rxjs';
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
/** Transient banner shown on a failed poll; cleared on the next successful poll. */
const LOST_CONTACT = 'Lost contact with the run; retrying…';

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

  setMode(mode: StudioPersonaMode): void {
    this.mode.set(mode);
  }

  selectPersona(id: string): void {
    this.state.setPersonaId(id);
  }

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
          this.team.set(resp.team ?? null);
          const complete = (resp.team?.processes ?? []).filter((p) => p.status === 'complete');
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
    // Reset any prior error before the request (consistent with loadTeam).
    this.error.set(null);
    this.personaApi
      .getPersonas()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (resp) => {
          const personas = resp.personas ?? [];
          this.personas.set(personas);
          // Default the persona selection when none carried from the handoff.
          if (!this.selectedPersonaId() && personas.length > 0) {
            this.state.setPersonaId(personas[0].id);
          }
        },
        error: () => {
          this.error.set('Could not load personas.');
        },
      });
  }

  // ── Launch + live run ───────────────────────────────────────────────────

  launch(): void {
    const teamId = this.teamId();
    const personaId = this.selectedPersonaId();
    const processId = this.selectedProcessId();
    if (!teamId || !personaId || !processId || this.launching()) {
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
      .pipe(takeUntilDestroyed(this.destroyRef))
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
          // The interval poll will retry; surface a transient banner meanwhile.
          this.error.set('Lost contact with the run; retrying…');
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

  newPersona(): void {
    const ref = this.dialog.open<
      PersonaEditorDialogComponent,
      PersonaEditorDialogData,
      PersonaEditorDialogResult
    >(PersonaEditorDialogComponent, { data: { mode: 'create' }, width: '560px' });
    // Close the dialog if the component is destroyed (e.g. the stepper moves to
    // another stage) so it isn't orphaned in the overlay.
    this.destroyRef.onDestroy(() => ref.close());
    ref
      .afterClosed()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((result) => {
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
              this.error.set('Could not create the persona.');
            },
          });
      });
  }

  // ── Back-loops (programmatic; the stepper stays forward-only) ─────────────

  iterateRoster(): void {
    this.state.navigateToStage(2); // Stage 3 — Compose Team
  }

  fixAgent(): void {
    if (this.canFixAgent()) {
      this.state.navigateToStage(1); // Stage 2 — Test Agent
    }
  }

  finishInCompose(): void {
    this.state.navigateToStage(2);
  }
}
