import { Component, inject, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { PersonaTestingApiService } from '../../services/persona-testing-api.service';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import type { PersonaInfo, PersonaTestRun } from '../../models';

const POLL_RUNS_MS = 15_000;
const TERMINAL_STATUSES = ['completed', 'failed'];

@Component({
  selector: 'app-persona-testing-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonModule,
    MatIconModule,
    MatCardModule,
    MatProgressBarModule,
    DashboardShellComponent,
  ],
  templateUrl: './persona-testing-dashboard.component.html',
  styleUrl: './persona-testing-dashboard.component.scss',
})
export class PersonaTestingDashboardComponent implements OnInit, OnDestroy {
  private readonly api = inject(PersonaTestingApiService);
  private readonly router = inject(Router);
  private runsSub: Subscription | null = null;

  personas: PersonaInfo[] = [];
  allRuns: PersonaTestRun[] = [];
  runningRuns: PersonaTestRun[] = [];
  completedRuns: PersonaTestRun[] = [];
  starting = false;
  startError: string | null = null;

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
}
