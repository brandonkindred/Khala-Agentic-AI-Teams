import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatTabsModule } from '@angular/material/tabs';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { SalesApiService } from '../../services/sales-api.service';
import { SalesPipelineFormComponent } from '../sales-pipeline-form/sales-pipeline-form.component';
import { SalesPipelineResultsComponent } from '../sales-pipeline-results/sales-pipeline-results.component';
import { SalesJobsPanelComponent } from '../sales-jobs-panel/sales-jobs-panel.component';
import { SalesLearningPanelComponent } from '../sales-learning-panel/sales-learning-panel.component';
import type { SalesPipelineJobListItem, SalesPipelineResult, SalesPipelineStatusResponse } from '../../models';

const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled'] as const;
const POLL_JOBS_MS = 12000;
const POLL_STATUS_MS = 2000;

@Component({
  selector: 'app-sales-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    MatTabsModule,
    MatCardModule,
    MatIconModule,
    MatButtonModule,
    MatSnackBarModule,
    SalesPipelineFormComponent,
    SalesPipelineResultsComponent,
    SalesJobsPanelComponent,
    SalesLearningPanelComponent,
  ],
  templateUrl: './sales-dashboard.component.html',
  styleUrl: './sales-dashboard.component.scss',
})
export class SalesDashboardComponent implements OnInit, OnDestroy {
  private readonly api = inject(SalesApiService);
  private readonly snackBar = inject(MatSnackBar);

  private jobsSub: Subscription | null = null;
  private statusPollSub: Subscription | null = null;

  jobs: SalesPipelineJobListItem[] = [];
  selectedJobId: string | null = null;
  selectedJobStatus: SalesPipelineStatusResponse | null = null;
  selectedResult: SalesPipelineResult | null = null;
  activeTabIndex = 0; // 0=Pipeline, 1=Results, 2=Jobs, 3=Learning

  isTerminal(status: string): boolean {
    return (TERMINAL_STATUSES as readonly string[]).includes(status);
  }

  ngOnInit(): void {
    this.jobsSub = timer(0, POLL_JOBS_MS).pipe(
      switchMap(() => this.api.listPipelineJobs())
    ).subscribe({
      next: (jobs) => {
        this.jobs = jobs;
        // If selected job disappeared, clear selection
        if (this.selectedJobId && !jobs.find(j => j.job_id === this.selectedJobId)) {
          this.clearSelection();
        }
      },
    });
  }

  ngOnDestroy(): void {
    this.jobsSub?.unsubscribe();
    this.statusPollSub?.unsubscribe();
  }

  onPipelineStarted(jobId: string): void {
    this.selectedJobId = jobId;
    this.selectedResult = null;
    this.startStatusPoll(jobId);
    // Optimistically add the job to the list
    this.api.listPipelineJobs().subscribe({
      next: (jobs) => { this.jobs = jobs; },
    });
  }

  onJobSelected(jobId: string): void {
    if (this.selectedJobId === jobId) return;
    this.selectedJobId = jobId;
    this.selectedResult = null;
    this.statusPollSub?.unsubscribe();
    const job = this.jobs.find(j => j.job_id === jobId);
    if (job && this.isTerminal(job.status)) {
      this.loadJobResult(jobId);
    } else {
      this.startStatusPoll(jobId);
    }
  }

  onJobDeleted(jobId: string): void {
    this.api.deleteJob(jobId).subscribe({
      next: () => {
        if (this.selectedJobId === jobId) this.clearSelection();
        this.api.listPipelineJobs().subscribe({ next: (jobs) => { this.jobs = jobs; } });
      },
      error: (err) => {
        this.snackBar.open(err?.error?.detail ?? 'Failed to delete job.', 'Dismiss', { duration: 3000 });
      },
    });
  }

  private startStatusPoll(jobId: string): void {
    this.statusPollSub?.unsubscribe();
    this.statusPollSub = timer(0, POLL_STATUS_MS).pipe(
      switchMap(() => this.api.getPipelineStatus(jobId))
    ).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        // Update the job in the list with fresh progress
        this.jobs = this.jobs.map(j =>
          j.job_id === jobId
            ? { ...j, status: status.status, progress: status.progress, current_stage: status.current_stage }
            : j
        );
        if (this.isTerminal(status.status)) {
          this.statusPollSub?.unsubscribe();
          if (status.status === 'completed') {
            this.loadJobResult(jobId);
          } else if (status.status === 'failed') {
            this.snackBar.open(`Pipeline failed: ${status.error ?? 'Unknown error'}`, 'Dismiss', { duration: 5000 });
          }
        }
      },
    });
  }

  private loadJobResult(jobId: string): void {
    this.api.getPipelineStatus(jobId).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        if (status.result) {
          this.selectedResult = status.result;
          this.activeTabIndex = 1; // Auto-switch to Results tab
        }
      },
    });
  }

  private clearSelection(): void {
    this.selectedJobId = null;
    this.selectedJobStatus = null;
    this.selectedResult = null;
    this.statusPollSub?.unsubscribe();
    this.statusPollSub = null;
  }
}
