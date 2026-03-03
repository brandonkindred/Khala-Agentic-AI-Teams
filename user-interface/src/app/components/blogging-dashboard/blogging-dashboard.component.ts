import { Component, inject, OnDestroy, OnInit } from '@angular/core';
import { CommonModule, SlicePipe } from '@angular/common';
import { MatTabsModule } from '@angular/material/tabs';
import { MatCardModule } from '@angular/material/card';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { BloggingApiService } from '../../services/blogging-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { ResearchReviewFormComponent } from '../research-review-form/research-review-form.component';
import { ResearchReviewResultsComponent } from '../research-review-results/research-review-results.component';
import { FullPipelineFormComponent } from '../full-pipeline-form/full-pipeline-form.component';
import { FullPipelineResultsComponent } from '../full-pipeline-results/full-pipeline-results.component';
import type {
  ResearchAndReviewRequest,
  ResearchAndReviewResponse,
  FullPipelineRequest,
  FullPipelineResponse,
  BlogJobListItem,
  BlogJobStatusResponse,
} from '../../models';

/**
 * Blogging API dashboard: research-and-review and full-pipeline forms and results.
 * Shows a running-jobs panel with job details when pipeline jobs are in progress.
 */
@Component({
  selector: 'app-blogging-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    SlicePipe,
    MatTabsModule,
    MatCardModule,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    HealthIndicatorComponent,
    ResearchReviewFormComponent,
    ResearchReviewResultsComponent,
    FullPipelineFormComponent,
    FullPipelineResultsComponent,
  ],
  templateUrl: './blogging-dashboard.component.html',
  styleUrl: './blogging-dashboard.component.scss',
})
export class BloggingDashboardComponent implements OnInit, OnDestroy {
  private readonly api = inject(BloggingApiService);
  private runningJobsSub: Subscription | null = null;
  private statusPollSub: Subscription | null = null;

  loading = false;
  error: string | null = null;
  researchReviewResult: ResearchAndReviewResponse | null = null;
  fullPipelineResult: FullPipelineResponse | null = null;

  /** Running blog pipeline jobs from GET /jobs (running_only=true). */
  runningJobs: BlogJobListItem[] = [];
  /** Job selected in the running-jobs panel. */
  selectedBlogJob: BlogJobListItem | null = null;
  /** Status for the selected job (polled via GET /job/{job_id}). */
  selectedJobStatus: BlogJobStatusResponse | null = null;

  /** Health check for the indicator. */
  healthCheck = (): ReturnType<BloggingApiService['health']> => this.api.health();

  ngOnInit(): void {
    this.runningJobsSub = timer(0, 30000).pipe(
      switchMap(() => this.api.getJobs(true))
    ).subscribe({
      next: (jobs) => {
        this.runningJobs = jobs;
        if (this.runningJobs.length === 0) {
          this.selectedBlogJob = null;
          this.selectedJobStatus = null;
          this.statusPollSub?.unsubscribe();
          this.statusPollSub = null;
        } else if (
          this.selectedBlogJob &&
          !this.runningJobs.find((j) => j.job_id === this.selectedBlogJob!.job_id)
        ) {
          this.selectedBlogJob = null;
          this.selectedJobStatus = null;
          this.statusPollSub?.unsubscribe();
          this.statusPollSub = null;
        } else if (this.runningJobs.length > 0 && !this.selectedBlogJob) {
          this.selectJob(this.runningJobs[0]);
        }
      },
    });
  }

  ngOnDestroy(): void {
    this.runningJobsSub?.unsubscribe();
    this.statusPollSub?.unsubscribe();
  }

  selectJob(job: BlogJobListItem): void {
    this.selectedBlogJob = job;
    this.statusPollSub?.unsubscribe();
    this.statusPollSub = timer(0, 8000).pipe(
      switchMap(() => this.api.getJobStatus(job.job_id))
    ).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
      },
    });
  }

  onResearchReviewSubmit(request: ResearchAndReviewRequest): void {
    this.loading = true;
    this.error = null;
    this.researchReviewResult = null;
    this.api.startResearchReviewAsync(request).subscribe({
      next: (res) => {
        this.loading = false;
        this.api.getJobs(true).subscribe((jobs) => {
          this.runningJobs = jobs;
          const j = jobs.find((x) => x.job_id === res.job_id);
          if (j) {
            this.selectJob(j);
          } else {
            this.runningJobs = [
              ...this.runningJobs,
              {
                job_id: res.job_id,
                status: 'running',
                brief: request.brief.slice(0, 100),
                progress: 0,
              },
            ];
            this.selectJob(this.runningJobs[this.runningJobs.length - 1]);
          }
        });
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Request failed';
        this.loading = false;
      },
    });
  }

  onFullPipelineSubmit(request: FullPipelineRequest): void {
    this.loading = true;
    this.error = null;
    this.fullPipelineResult = null;
    this.api.startFullPipelineAsync(request).subscribe({
      next: (res) => {
        this.loading = false;
        this.api.getJobs(true).subscribe((jobs) => {
          this.runningJobs = jobs;
          const j = jobs.find((x) => x.job_id === res.job_id);
          if (j) {
            this.selectJob(j);
          } else {
            this.runningJobs = [
              ...this.runningJobs,
              {
                job_id: res.job_id,
                status: 'running',
                brief: request.brief.slice(0, 100),
                progress: 0,
              },
            ];
            this.selectJob(this.runningJobs[this.runningJobs.length - 1]);
          }
        });
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Request failed';
        this.loading = false;
      },
    });
  }
}
