import { Component, inject, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatTooltipModule } from '@angular/material/tooltip';
import { PersonaTestingApiService } from '../../services/persona-testing-api.service';
import { JobActionsService } from '../../services/job-actions.service';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import type { JobSource, PersonaInfo, PersonaTestRun } from '../../models';

const TEAM_SOURCE: JobSource = 'user_agent_founder';
const POLL_RUNS_MS = 15_000;
const TERMINAL_STATUSES = ['completed', 'failed'];
const RESUMABLE_STATUSES = new Set<string>(['failed', 'interrupted', 'agent_crash']);

@Component({
  selector: 'app-persona-testing-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonModule,
    MatIconModule,
    MatCardModule,
    MatProgressBarModule,
    MatTooltipModule,
    DashboardShellComponent,
  ],
  templateUrl: './persona-testing-dashboard.component.html',
  styleUrl: './persona-testing-dashboard.component.scss',
})
export class PersonaTestingDashboardComponent implements OnInit, OnDestroy {
  private readonly api = inject(PersonaTestingApiService);
  private readonly jobActions = inject(JobActionsService);
  private readonly router = inject(Router);
  private runsSub: Subscription | null = null;

  personas: PersonaInfo[] = [];
  allRuns: PersonaTestRun[] = [];
  runningRuns: PersonaTestRun[] = [];
  completedRuns: PersonaTestRun[] = [];
  starting = false;
  startError: string | null = null;
  actionPending = new Set<string>();
  actionError: string | null = null;

  ngOnInit(): void {
    this.api.getPersonas().subscribe({
      next: (resp) => (this.personas = resp.personas),
    });

    this.runsSub = timer(0, POLL_RUNS_MS)
      .pipe(switchMap(() => this.api.getRuns()))
      .subscribe({
        next: (resp) => {
          this.allRuns = resp.runs;
          this.runningRuns = this.allRuns.filter((r) => !TERMINAL_STATUSES.includes(r.status));
          this.completedRuns = this.allRuns.filter((r) => TERMINAL_STATUSES.includes(r.status));
        },
      });
  }

  ngOnDestroy(): void {
    this.runsSub?.unsubscribe();
  }

  startTest(): void {
    this.starting = true;
    this.startError = null;
    this.api.startTest().subscribe({
      next: (resp) => {
        this.starting = false;
        this.router.navigate(['/persona-testing/audit', resp.run_id]);
      },
      error: (err) => {
        this.starting = false;
        this.startError = err?.error?.detail ?? 'Failed to start test';
      },
    });
  }

  openAudit(runId: string): void {
    this.router.navigate(['/persona-testing/audit', runId]);
  }

  formatStatus(status: string): string {
    return status.replace(/_/g, ' ');
  }

  canStop(run: PersonaTestRun): boolean {
    // Any non-terminal status is cancellable — this mirrors the backend's
    // ``_cancellable_statuses()`` gate and avoids drift when the orchestrator
    // adds new intermediate phases (e.g. ``answering_analysis_questions``).
    return !TERMINAL_STATUSES.includes(run.status);
  }

  canResume(run: PersonaTestRun): boolean {
    return RESUMABLE_STATUSES.has(run.status);
  }

  canRestart(run: PersonaTestRun): boolean {
    return TERMINAL_STATUSES.includes(run.status) || RESUMABLE_STATUSES.has(run.status);
  }

  private refreshRuns(): void {
    this.api.getRuns().subscribe({
      next: (resp) => {
        this.allRuns = resp.runs;
        this.runningRuns = this.allRuns.filter((r) => !TERMINAL_STATUSES.includes(r.status));
        this.completedRuns = this.allRuns.filter((r) => TERMINAL_STATUSES.includes(r.status));
      },
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
