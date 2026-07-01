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
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { Subject, catchError, map, of, switchMap } from 'rxjs';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import { AgentStudioStateService } from '../../services/agent-studio-state.service';
import { ProcessDesignerChatComponent } from '../process-designer-chat/process-designer-chat.component';
import type {
  AgenticTeam,
  AgenticTeamSummary,
  RosterValidationResult,
} from '../../models';

/**
 * Agent Studio — Stage 3 "Compose Team" (spec §3, Stage 3).
 *
 * Lets the user pick (or create) a team, then reuses `app-process-designer-chat`
 * as-is for the chat-driven process design *and* the roster panel (which this
 * increment extends with add-from-registry / delete / inline-edit — spec §5.3).
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
    ProcessDesignerChatComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-compose-team.component.html',
  styleUrl: './agent-studio-compose-team.component.scss',
})
export class AgentStudioComposeTeamComponent implements OnInit {
  private readonly state = inject(AgentStudioStateService);
  private readonly api = inject(AgenticTeamApiService);
  private readonly fb = inject(FormBuilder);
  private readonly destroyRef = inject(DestroyRef);

  readonly teams = signal<AgenticTeamSummary[]>([]);
  readonly teamsLoading = signal(false);
  readonly teamsError = signal<string | null>(null);

  readonly team = signal<AgenticTeam | null>(null);
  readonly teamLoadError = signal<string | null>(null);

  readonly showCreateForm = signal(false);
  readonly creating = signal(false);
  readonly createError = signal<string | null>(null);

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
   */
  private readonly teamFetch = new Subject<string>();

  constructor() {
    this.teamFetch
      .pipe(
        switchMap((teamId) =>
          this.api.getTeam(teamId).pipe(
            map((resp) => ({ ok: true as const, team: resp?.team ?? null })),
            catchError(() => of({ ok: false as const, team: null })),
          ),
        ),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe((res) => {
        if (!res.ok) {
          this.teamLoadError.set('Could not load this team.');
          return;
        }
        if (!res.team) {
          this.teamLoadError.set('Team not found.');
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
    this.api
      .listTeams()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (teams) => {
        this.teamsLoading.set(false);
        this.teams.set(teams);
      },
      error: (err) => {
        this.teamsLoading.set(false);
        this.teamsError.set(err?.error?.detail ?? 'Failed to load teams');
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
    this.teamLoadError.set(null);
    this.teamFetch.next(teamId);
  }

  private applyTeam(team: AgenticTeam): void {
    this.team.set(team);
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
    this.teamFetch.next(teamId);
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
    this.api
      .createTeam({ name, description })
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
          this.createError.set(err?.error?.detail ?? 'Failed to create team');
        },
      });
  }
}
