import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatCardModule } from '@angular/material/card';
import { Subject, Subscription, timer, of } from 'rxjs';
import { catchError, startWith, switchMap, tap } from 'rxjs/operators';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import { SalesJobsPanelComponent } from '../sales-jobs-panel/sales-jobs-panel.component';
import { SalesPipelineResultsComponent } from '../sales-pipeline-results/sales-pipeline-results.component';
import { SalesApiService } from '../../services/sales-api.service';
import type {
  SalesPipelineJobListItem,
  SalesPipelineStatusResponse,
} from '../../models';

const JOBS_POLL_MS = 15000;
const STATUS_POLL_MS = 10000;
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled']);

@Component({
  selector: 'app-sales-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    MatIconModule,
    MatButtonModule,
    MatProgressBarModule,
    MatCardModule,
    TeamAssistantChatComponent,
    DashboardShellComponent,
    SalesJobsPanelComponent,
    SalesPipelineResultsComponent,
  ],
  templateUrl: './sales-dashboard.component.html',
  styleUrl: './sales-dashboard.component.scss',
})
export class SalesDashboardComponent implements OnInit, OnDestroy {
  private readonly api = inject(SalesApiService);

  view: 'chat' | 'jobs' = 'chat';

  jobs: SalesPipelineJobListItem[] = [];
  selectedJobId: string | null = null;
  selectedJobStatus: SalesPipelineStatusResponse | null = null;

  listError: string | null = null;
  statusError: string | null = null;
  cancelling = false;

  private readonly refreshTrigger$ = new Subject<void>();
  private jobsSub: Subscription | null = null;
  private statusSub: Subscription | null = null;
  private initialJobsLoad = true;

  // --- Lifecycle ---------------------------------------------------------

  ngOnInit(): void {
    this.jobsSub = this.refreshTrigger$
      .pipe(
        startWith(undefined as void),
        switchMap(() => timer(0, JOBS_POLL_MS)),
        switchMap(() =>
          this.api.listPipelineJobs(false).pipe(
            // Clear the error banner only on a successful fetch — a fallback
            // emission from catchError below must not wipe the error we just set.
            tap(() => {
              this.listError = null;
            }),
            catchError((err) => {
              this.listError = err?.error?.detail ?? err?.message ?? 'Failed to load jobs';
              return of([] as SalesPipelineJobListItem[]);
            }),
          ),
        ),
      )
      .subscribe((jobs) => {
        this.jobs = jobs;
        // Auto-land on the jobs view only on the very first emission — never on
        // later poll ticks, so the user can freely navigate back to chat
        // without getting bounced back every 15 seconds.
        if (this.initialJobsLoad) {
          this.initialJobsLoad = false;
          if (this.view === 'chat' && jobs.length > 0) {
            this.view = 'jobs';
          }
        }
        // If the selected job got deleted upstream, clear it.
        if (this.selectedJobId && !jobs.find((j) => j.job_id === this.selectedJobId)) {
          this.clearSelectedJob();
        }
      });
  }

  ngOnDestroy(): void {
    this.jobsSub?.unsubscribe();
    this.statusSub?.unsubscribe();
    this.refreshTrigger$.complete();
  }

  // --- Navigation --------------------------------------------------------

  showChat(): void {
    this.view = 'chat';
  }

  showJobs(): void {
    this.view = 'jobs';
  }

  // --- Chat → backend launch → jobs view --------------------------------

  onWorkflowLaunched(event: {
    job_id: string | null;
    conversation_id: string;
    upstream_status: number;
    upstream_body: Record<string, unknown>;
  }): void {
    // Refresh the jobs list immediately so the new job appears without
    // waiting for the next poll tick.
    this.refreshTrigger$.next();
    if (event.job_id) {
      this.view = 'jobs';
      this.selectJob(event.job_id);
    }
  }

  // --- Jobs panel interactions ------------------------------------------

  selectJob(jobId: string): void {
    if (this.selectedJobId === jobId) return;
    this.selectedJobId = jobId;
    this.selectedJobStatus = null;
    this.statusError = null;
    this.startStatusPolling(jobId);
  }

  deleteJob(jobId: string): void {
    if (!confirm('Delete this pipeline run? This cannot be undone.')) return;
    this.api.deleteJob(jobId).subscribe({
      next: () => {
        if (this.selectedJobId === jobId) this.clearSelectedJob();
        this.refreshTrigger$.next();
      },
      error: (err) => {
        this.listError = err?.error?.detail ?? err?.message ?? 'Failed to delete job';
      },
    });
  }

  cancelSelectedJob(): void {
    const jobId = this.selectedJobId;
    if (!jobId || this.cancelling) return;
    this.cancelling = true;
    this.api.cancelJob(jobId).subscribe({
      next: () => {
        this.cancelling = false;
        this.refreshTrigger$.next();
      },
      error: (err) => {
        this.cancelling = false;
        this.statusError = err?.error?.detail ?? err?.message ?? 'Failed to cancel job';
      },
    });
  }

  // --- Selected-job status polling --------------------------------------

  private startStatusPolling(jobId: string): void {
    this.statusSub?.unsubscribe();
    this.statusSub = timer(0, STATUS_POLL_MS)
      .pipe(
        switchMap(() =>
          this.api.getPipelineStatus(jobId).pipe(
            catchError((err) => {
              this.statusError =
                err?.error?.detail ?? err?.message ?? 'Failed to load job status';
              return of<SalesPipelineStatusResponse | null>(null);
            }),
          ),
        ),
      )
      .subscribe((status) => {
        if (!status || this.selectedJobId !== jobId) return;
        this.statusError = null;
        this.selectedJobStatus = status;
        if (this.isTerminal(status.status)) {
          this.statusSub?.unsubscribe();
          this.statusSub = null;
        }
      });
  }

  private clearSelectedJob(): void {
    this.selectedJobId = null;
    this.selectedJobStatus = null;
    this.statusError = null;
    this.statusSub?.unsubscribe();
    this.statusSub = null;
  }

  // --- Helpers -----------------------------------------------------------

  isTerminal(status: string | undefined): boolean {
    return !!status && TERMINAL_STATUSES.has(status);
  }

  stageLabel(stage: string | undefined): string {
    if (!stage) return '';
    return stage.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  get runningJobsCount(): number {
    return this.jobs.filter((j) => j.status === 'running' || j.status === 'pending').length;
  }
}
