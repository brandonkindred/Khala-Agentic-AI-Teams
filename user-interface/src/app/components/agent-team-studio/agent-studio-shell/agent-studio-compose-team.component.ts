import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  OnInit,
  ViewChild,
  computed,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { Subject, catchError, map, of, switchMap } from 'rxjs';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { AgentCatalogComponent } from '../agent-console/agent-catalog/agent-catalog.component';
import { AgentStudioFacade } from '../../../services/agent-studio.facade';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentStudioSlideOutComponent } from './agent-studio-slide-out/agent-studio-slide-out.component';
import { ProcessDesignerChatComponent } from '../../process-designer-chat/process-designer-chat.component';
import { ErrorMessageComponent } from '../../../shared/error-message/error-message.component';
import { extractErrorDetail } from '../../../shared/extract-error-detail';
import { STAGE_INDEX } from '../../../models/agent-studio.model';
import type {
  AgenticTeam,
  AgenticTeamAgent,
  AgenticTeamSummary,
  RosterValidationResult,
} from '../../../models';

/**
 * Agent Studio — Stage 3 "Compose Team" (spec §3, Stage 3).
 *
 * Lets the user pick (or create) a team, then reuses `app-process-designer-chat`
 * as-is for the chat-driven process design *and* the roster panel (which this
 * increment extends with add-from-registry / delete / inline-edit — spec §5.3).
 * Primary HTTP goes through `AgentStudioFacade`; the embedded chat keeps its
 * own API client (same documented exception as Stage 2's Console runner).
 *
 * A process selector beside the team picker (auto-selected when the team has
 * exactly one) chooses which process becomes the Stage-4 handoff target,
 * independent of whatever process the chat conversation happens to be actively
 * iterating on. This stage writes `teamId` / `processId` /
 * `rosterFullyStaffed` / `composeProcessStatus` into the shared studio state;
 * the shell reads the latter two to gate "Test this team →".
 */
@Component({
  selector: 'app-agent-studio-compose-team',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    MatButtonModule,
    MatCardModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatSelectModule,
    AgentCatalogComponent,
    AgentStudioSlideOutComponent,
    ProcessDesignerChatComponent,
    ErrorMessageComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-compose-team.component.html',
  styleUrl: './agent-studio-compose-team.component.scss',
})
export class AgentStudioComposeTeamComponent implements OnInit {
  private readonly state = inject(AgentStudioStateService);
  private readonly facade = inject(AgentStudioFacade);
  private readonly fb = inject(FormBuilder);
  private readonly destroyRef = inject(DestroyRef);

  /** The embedded chat/roster panel — used to refresh its roster after an auto-add. */
  @ViewChild(ProcessDesignerChatComponent) private chat?: ProcessDesignerChatComponent;

  readonly teams = signal<AgenticTeamSummary[]>([]);
  readonly teamsLoading = signal(false);
  readonly teamsError = signal<string | null>(null);

  readonly team = signal<AgenticTeam | null>(null);
  readonly teamLoadError = signal<string | null>(null);

  readonly showCreateForm = signal(false);
  readonly creating = signal(false);
  readonly createError = signal<string | null>(null);

  /** Whether the "Browse agents" overlay is open (spec §2.1). */
  readonly browseOpen = signal(false);

  readonly selectedTeamId = computed(() => this.state.teamId());
  readonly selectedProcessId = computed(() => this.state.processId());

  form = this.fb.nonNullable.group({
    name: ['', [Validators.required, Validators.minLength(1), Validators.maxLength(200)]],
    description: ['', [Validators.maxLength(1000)]],
  });

  /**
   * Team-fetch stream. Routing every `getTeam` (team select, roster-change
   * re-sync) through one `switchMap` cancels a prior in-flight fetch, so
   * switching teams rapidly can't let an earlier team's response land after a
   * later one and overwrite `team` with stale data.
   *
   * `surfaceError` distinguishes a user-initiated load (team select / initial)
   * from a background re-sync (`onRosterChanged`): only the former surfaces a
   * full-stage `teamLoadError` on failure. A background re-sync that blips must
   * NOT set `teamLoadError` — the template hides the whole chat/roster when it's
   * truthy, so doing so would tear down a working, mid-conversation stage over a
   * transient failure (and, since the chat then can't re-emit `rosterChanged`,
   * leave it stuck).
   */
  private readonly teamFetch = new Subject<{ teamId: string; surfaceError: boolean }>();

  constructor() {
    this.teamFetch
      .pipe(
        switchMap(({ teamId, surfaceError }) =>
          this.facade.getTeam(teamId).pipe(
            // Cast to `AgenticTeam | null` so the defensive "no team in the body"
            // branch below stays reachable — the response type declares `team`
            // non-null, but an empty/HTTP-mapped-null body is still handled.
            map((resp) => ({
              ok: true as const,
              team: (resp?.team ?? null) as AgenticTeam | null,
              surfaceError,
            })),
            catchError(() => of({ ok: false as const, team: null, surfaceError })),
          ),
        ),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe((res) => {
        if (!res.ok) {
          if (res.surfaceError) this.teamLoadError.set('Could not load this team.');
          return;
        }
        if (!res.team) {
          if (res.surfaceError) this.teamLoadError.set('Team not found.');
          return;
        }
        this.applyTeam(res.team);
      });
  }

  ngOnInit(): void {
    this.loadTeams();
    const teamId = this.selectedTeamId();
    if (teamId) {
      this.loadTeam(teamId);
    }
  }

  private loadTeams(): void {
    this.teamsLoading.set(true);
    this.teamsError.set(null);
    this.facade
      .listTeams()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (teams) => {
        this.teamsLoading.set(false);
        this.teams.set(teams);
      },
      error: (err) => {
        this.teamsLoading.set(false);
        this.teamsError.set(extractErrorDetail(err, 'Failed to load teams'));
      },
      });
  }

  /** Select an existing team as the Stage-3 subject (clears any prior process gate state). */
  selectTeam(teamId: string): void {
    this.state.setTeamId(teamId);
    this.state.setProcessId(null);
    this.state.setRosterFullyStaffed(false);
    this.state.setComposeProcessStatus(null);
    this.team.set(null);
    this.loadTeam(teamId);
  }

  private loadTeam(teamId: string): void {
    // User-initiated load: clear any prior error and surface a new one on failure.
    this.teamLoadError.set(null);
    this.teamFetch.next({ teamId, surfaceError: true });
  }

  private applyTeam(team: AgenticTeam): void {
    this.team.set(team);
    this.consumeHandoffAgent(team);
    const current = this.selectedProcessId();
    const stillExists = !!current && team.processes.some((p) => p.process_id === current);
    if (stillExists) {
      // Re-sync the gate status in case the selected process's status changed
      // (e.g. the chat just marked it complete).
      this.state.setComposeProcessStatus(
        team.processes.find((p) => p.process_id === current)?.status ?? null,
      );
      return;
    }
    // No valid selection: auto-select the sole process, or clear the gate.
    this.selectProcess(team.processes.length === 1 ? team.processes[0].process_id : null);
  }

  /**
   * When the user reached Stage 3 via Stage 2's "Add to team →", add the tested
   * agent (handoff `registryAgentId`) to this team's roster so they don't have to
   * search for it again (spec §2.4 handoff). Idempotent:
   *   - at most one attempt per (team, handoff agent) — the façade owns the
   *     consumed-handoff set, so it holds across team switches, a background
   *     re-sync, a return visit to the team, AND the Stage-4 "iterate roster"
   *     back-loop that destroys and recreates this component; a manual delete
   *     is therefore never undone, and
   *   - skipped when the team already carries that manifest (no duplicate, and
   *     switching to a team that already has it is a no-op).
   * `registryAgentId` is left set so Stage 4's "fix an agent" back-loop still works.
   *
   * Preconditions: `team` is the currently loaded team (non-null).
   * Postconditions: at most one façade `addAgentToTeam` call is in flight per
   *   `(teamId, registryAgentId)` this session; `registryAgentId` is unchanged.
   */
  private consumeHandoffAgent(team: AgenticTeam): void {
    const manifestId = this.state.registryAgentId();
    if (!manifestId) {
      return;
    }
    const key = `${team.team_id}::${manifestId}`;
    if (this.state.hasConsumedHandoff(key)) {
      return; // already attempted for this (team, agent) — don't re-add after a delete
    }
    this.facade
      .addAgentToTeam(
        team.team_id,
        manifestId,
        team.agents.some((a) => a.manifest_id === manifestId),
      )
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        // Reflect the new agent in the roster panel (and re-evaluate the gate).
        // The child owns its roster view, so ask it to reload rather than mutating
        // its state from here. A null emission means the façade skipped the POST
        // (already consumed, or already on the roster).
        next: (added) => {
          if (added) this.chat?.refreshRoster();
        },
        // Best-effort: on failure the user can still add it manually via the panel.
        error: () => undefined,
      });
  }

  selectProcess(processId: string | null): void {
    this.state.setProcessId(processId);
    const process = processId
      ? this.team()?.processes.find((p) => p.process_id === processId)
      : null;
    this.state.setComposeProcessStatus(process?.status ?? null);
  }

  /** Wired to `app-process-designer-chat`'s `(rosterChanged)` (spec §3, Stage 3). */
  onRosterChanged(validation: RosterValidationResult | null): void {
    this.state.setRosterFullyStaffed(!!validation?.is_fully_staffed);
    // The chat may have created/edited a process as a side effect (e.g. its own
    // "Create New Process" action) — refresh so the process selector and gate
    // status stay in sync without a manual reload.
    const teamId = this.selectedTeamId();
    if (!teamId) return;
    // Background re-sync: don't surface a full-stage error on a transient blip —
    // it would tear down the working chat/roster (see `teamFetch` doc).
    this.teamFetch.next({ teamId, surfaceError: false });
  }

  toggleCreateForm(): void {
    this.showCreateForm.update((v) => !v);
    if (!this.showCreateForm()) {
      this.form.reset({ name: '', description: '' });
      this.createError.set(null);
    }
  }

  onCreateTeam(): void {
    if (this.form.invalid || this.creating()) return;
    this.creating.set(true);
    this.createError.set(null);
    const { name, description } = this.form.getRawValue();
    this.facade
      .composeTeam({ name, description })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (resp) => {
          this.creating.set(false);
          this.showCreateForm.set(false);
          this.form.reset({ name: '', description: '' });
          this.loadTeams();
          this.selectTeam(resp.team_id);
        },
        error: (err) => {
          this.creating.set(false);
          this.createError.set(extractErrorDetail(err, 'Failed to create team'));
        },
      });
  }

  // ── Browse agents / Test ▸ (spec §2.1) ─────────────────────────────────────

  openBrowse(): void {
    this.browseOpen.set(true);
  }

  closeBrowse(): void {
    this.browseOpen.set(false);
  }

  /**
   * Re-point the handoff agent to `id` from the Browse-agents overlay. This
   * only updates `registryAgentId` as a candidate for later actions (e.g.
   * "+ Add" or a Stage-4 "fix an agent") — it must NOT auto-add `id` to the
   * roster (`consumeHandoffAgent` only fires from a team *load*, not from
   * this handoff-id change, so no extra guard is needed here).
   */
  onBrowseSelect(id: string): void {
    this.state.setRegistryAgentId(id);
    this.closeBrowse();
  }

  /**
   * Per-roster-entry `Test ▸` back-loop (spec §2.1): open `agent` in Stage 2's
   * sandbox. Registry-only — the template only renders this action enabled for
   * `source: 'registry'` entries, which carry a real `manifest_id`.
   */
  onTestAgent(agent: AgenticTeamAgent): void {
    this.state.setRegistryAgentId(agent.manifest_id);
    this.state.navigateToStage(STAGE_INDEX.test);
  }
}
