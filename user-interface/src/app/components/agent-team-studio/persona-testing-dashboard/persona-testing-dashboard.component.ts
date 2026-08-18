import { Component, inject, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatDialog, MatDialogModule } from '@angular/material/dialog';
import { PersonaTestingApiService } from '../../../services/persona-testing-api.service';
import { JobActionsService } from '../../../services/job-actions.service';
import { DashboardShellComponent } from '../../../shared/dashboard-shell/dashboard-shell.component';
import { isPersonaRunTerminal } from '../../../models';
import type {
  JobSource,
  PersonaInfo,
  PersonaTestRun,
  TestableTeam,
} from '../../../models';
import {
  PersonaEditorDialogComponent,
  PersonaEditorDialogData,
  PersonaEditorDialogResult,
} from './persona-editor-dialog.component';
import {
  StartTestDialogComponent,
  StartTestDialogData,
  StartTestDialogResult,
} from './start-test-dialog.component';

const TEAM_SOURCE: JobSource = 'user_agent_founder';
const POLL_RUNS_MS = 15_000;
const RESUMABLE_STATUSES = new Set<string>(['failed', 'interrupted', 'agent_crash']);
// Dynamic agentic-team target keys (`agentic_team:<id>`). This legacy dialog has
// no process selector and can't supply the `process_id` those targets require, so
// they're filtered out here — agentic teams are launched from Agent Studio, which
// has a dedicated process picker. The backend `/testable-teams` still lists them.
const AGENTIC_TEAM_PREFIX = 'agentic_team:';

@Component({
  selector: 'app-persona-testing-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonModule,
    MatIconModule,
    MatCardModule,
    MatChipsModule,
    MatProgressBarModule,
    MatTooltipModule,
    MatDialogModule,
    DashboardShellComponent,
  ],
  templateUrl: './persona-testing-dashboard.component.html',
  styleUrl: './persona-testing-dashboard.component.scss',
})
export class PersonaTestingDashboardComponent implements OnInit, OnDestroy {
  private readonly api = inject(PersonaTestingApiService);
  private readonly jobActions = inject(JobActionsService);
  private readonly router = inject(Router);
  private readonly dialog = inject(MatDialog);
  private runsSub: Subscription | null = null;

  personas: PersonaInfo[] = [];
  teams: TestableTeam[] = [];
  allRuns: PersonaTestRun[] = [];
  runningRuns: PersonaTestRun[] = [];
  completedRuns: PersonaTestRun[] = [];
  starting = false;
  startError: string | null = null;
  personaError: string | null = null;
  actionPending = new Set<string>();
  actionError: string | null = null;

  ngOnInit(): void {
    this.refreshPersonas();
    this.api.getTestableTeams().subscribe({
      next: (resp) =>
        (this.teams = (resp?.teams ?? []).filter(
          (t) => !t.team_key.startsWith(AGENTIC_TEAM_PREFIX),
        )),
      // Degrade gracefully on a failed load: leave the team list empty (the
      // start dialog short-circuits when there are no teams) rather than letting
      // the error surface unhandled.
      error: () => {
        this.teams = [];
      },
    });

    this.runsSub = timer(0, POLL_RUNS_MS)
      .pipe(switchMap(() => this.api.getRuns()))
      .subscribe({
        next: (resp) => this.applyRuns(resp.runs),
      });
  }

  ngOnDestroy(): void {
    this.runsSub?.unsubscribe();
  }

  // ── Persona CRUD ─────────────────────────────────────────────────

  openCreateDialog(): void {
    const ref = this.dialog.open<
      PersonaEditorDialogComponent,
      PersonaEditorDialogData,
      PersonaEditorDialogResult
    >(PersonaEditorDialogComponent, {
      data: { mode: 'create' },
      width: '720px',
    });
    ref.afterClosed().subscribe((result) => this.onCreateDialogClosed(result));
  }

  /** Public for unit tests; invoked by `openCreateDialog` after the dialog closes. */
  onCreateDialogClosed(result: PersonaEditorDialogResult | undefined): void {
    if (!result) return;
    this.personaError = null;
    this.api.createPersona(result).subscribe({
      next: () => this.refreshPersonas(),
      error: (err) => {
        this.personaError = err?.error?.detail ?? 'Failed to create persona';
      },
    });
  }

  openEditDialog(persona: PersonaInfo): void {
    const ref = this.dialog.open<
      PersonaEditorDialogComponent,
      PersonaEditorDialogData,
      PersonaEditorDialogResult
    >(PersonaEditorDialogComponent, {
      data: { mode: 'edit', persona },
      width: '720px',
    });
    ref.afterClosed().subscribe((result) => this.onEditDialogClosed(persona, result));
  }

  /** Public for unit tests; invoked by `openEditDialog` after the dialog closes. */
  onEditDialogClosed(
    persona: PersonaInfo,
    result: PersonaEditorDialogResult | undefined,
  ): void {
    if (!result) return;
    this.personaError = null;
    this.api.updatePersona(persona.id, result).subscribe({
      next: () => this.refreshPersonas(),
      error: (err) => {
        this.personaError = err?.error?.detail ?? 'Failed to update persona';
      },
    });
  }

  deletePersona(persona: PersonaInfo): void {
    const confirmed = window.confirm(
      `Delete persona "${persona.name}"? This cannot be undone.` +
        (persona.is_builtin
          ? ' (Built-in personas re-seed on next API restart.)'
          : ''),
    );
    if (!confirmed) return;
    this.personaError = null;
    this.api.deletePersona(persona.id).subscribe({
      next: () => this.refreshPersonas(),
      error: (err) => {
        this.personaError = err?.error?.detail ?? 'Failed to delete persona';
      },
    });
  }

  // ── Start Test ───────────────────────────────────────────────────

  openStartTestDialog(initialPersonaId?: string): void {
    if (!this.personas.length || !this.teams.length) return;
    const ref = this.dialog.open<
      StartTestDialogComponent,
      StartTestDialogData,
      StartTestDialogResult
    >(StartTestDialogComponent, {
      data: {
        personas: this.personas,
        teams: this.teams,
        initialPersonaId,
      },
      width: '480px',
    });
    ref.afterClosed().subscribe((result) => this.onStartTestDialogClosed(result));
  }

  /** Public for unit tests; invoked by `openStartTestDialog` after the dialog closes. */
  onStartTestDialogClosed(result: StartTestDialogResult | undefined): void {
    if (!result) return;
    this.starting = true;
    this.startError = null;
    this.api.startTest(result).subscribe({
      next: (resp) => {
        this.starting = false;
        this.router.navigate(['/persona-testing/audit', resp.job_id]);
      },
      error: (err) => {
        this.starting = false;
        this.startError = err?.error?.detail ?? 'Failed to start test';
      },
    });
  }

  // ── Run lookups ──────────────────────────────────────────────────

  personaName(personaId: string | undefined): string {
    if (!personaId) return '—';
    return this.personas.find((p) => p.id === personaId)?.name ?? personaId;
  }

  teamName(teamKey: string | undefined): string {
    if (!teamKey) return '—';
    return this.teams.find((t) => t.team_key === teamKey)?.display_name ?? teamKey;
  }

  // ── Run actions / polling (unchanged behavior) ───────────────────

  openAudit(runId: string): void {
    this.router.navigate(['/persona-testing/audit', runId]);
  }

  formatStatus(status: string): string {
    return status.replace(/_/g, ' ');
  }

  canStop(run: PersonaTestRun): boolean {
    return !isPersonaRunTerminal(run.status);
  }

  canResume(run: PersonaTestRun): boolean {
    return RESUMABLE_STATUSES.has(run.status);
  }

  canRestart(run: PersonaTestRun): boolean {
    return isPersonaRunTerminal(run.status) || RESUMABLE_STATUSES.has(run.status);
  }

  private refreshPersonas(): void {
    this.api.getPersonas().subscribe({
      next: (resp) => (this.personas = resp.personas),
    });
  }

  private applyRuns(runs: PersonaTestRun[]): void {
    this.allRuns = runs;
    this.runningRuns = runs.filter((r) => !isPersonaRunTerminal(r.status));
    this.completedRuns = runs.filter((r) => isPersonaRunTerminal(r.status));
  }

  private refreshRuns(): void {
    this.api.getRuns().subscribe({
      next: (resp) => this.applyRuns(resp.runs),
    });
  }

  private dispatch(run: PersonaTestRun, action: 'stop' | 'resume' | 'restart' | 'delete'): void {
    const key = `${action}:${run.run_id}`;
    if (this.actionPending.has(key)) return;
    this.actionPending.add(key);
    this.actionError = null;

    const call$ =
      action === 'stop'
        ? this.jobActions.stop(TEAM_SOURCE, run.run_id)
        : action === 'resume'
          ? this.jobActions.resume(TEAM_SOURCE, run.run_id)
          : action === 'restart'
            ? this.jobActions.restart(TEAM_SOURCE, run.run_id)
            : this.jobActions.delete(TEAM_SOURCE, run.run_id);

    call$.subscribe({
      next: () => {
        this.actionPending.delete(key);
        this.refreshRuns();
      },
      error: (err) => {
        this.actionPending.delete(key);
        this.actionError = err?.error?.detail ?? `Failed to ${action} run ${run.run_id}`;
      },
    });
  }

  stopRun(run: PersonaTestRun, event: Event): void {
    event.stopPropagation();
    this.dispatch(run, 'stop');
  }

  resumeRun(run: PersonaTestRun, event: Event): void {
    event.stopPropagation();
    this.dispatch(run, 'resume');
  }

  restartRun(run: PersonaTestRun, event: Event): void {
    event.stopPropagation();
    this.dispatch(run, 'restart');
  }

  deleteRun(run: PersonaTestRun, event: Event): void {
    event.stopPropagation();
    this.dispatch(run, 'delete');
  }

  isActionPending(run: PersonaTestRun, action: 'stop' | 'resume' | 'restart' | 'delete'): boolean {
    return this.actionPending.has(`${action}:${run.run_id}`);
  }
}
