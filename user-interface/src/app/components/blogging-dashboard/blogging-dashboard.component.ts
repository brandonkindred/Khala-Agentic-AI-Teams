import { Component, inject, OnDestroy, OnInit } from '@angular/core';
import { CommonModule, SlicePipe } from '@angular/common';
import { MatTabsModule } from '@angular/material/tabs';
import { MatCardModule } from '@angular/material/card';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { BloggingApiService } from '../../services/blogging-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { MatListModule } from '@angular/material/list';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatChipsModule } from '@angular/material/chips';
import { MatDividerModule } from '@angular/material/divider';
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
 * Shows Jobs panel (running and completed) with job details and produced assets.
 */
const TERMINAL_STATUSES = ['completed', 'needs_human_review', 'failed'] as const;
const POLL_JOBS_MS = 12000;
const POLL_STATUS_MS = 8000;

export function artifactLabel(name: string): string {
  const labels: Record<string, string> = {
    'brand_spec.yaml': 'Brand spec',
    'content_brief.md': 'Content brief',
    'research_packet.md': 'Research packet',
    'allowed_claims.json': 'Allowed claims',
    'outline.md': 'Outline',
    'draft_v1.md': 'Draft v1',
    'draft_v2.md': 'Draft v2',
    'final.md': 'Final draft',
    'compliance_report.json': 'Compliance report',
    'validator_report.json': 'Validator report',
    'publishing_pack.json': 'Publishing pack',
  };
  return labels[name] ?? name;
}

@Component({
  selector: 'app-blogging-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    SlicePipe,
    MatTabsModule,
    MatCardModule,
    MatListModule,
    MatExpansionModule,
    MatChipsModule,
    MatDividerModule,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    ResearchReviewFormComponent,
    ResearchReviewResultsComponent,
    FullPipelineFormComponent,
    FullPipelineResultsComponent,
  ],
  templateUrl: './blogging-dashboard.component.html',
  styleUrl: './blogging-dashboard.component.scss',
})
export class BloggingDashboardComponent implements OnInit, OnDestroy {
  readonly artifactLabel = artifactLabel;
  private readonly api = inject(BloggingApiService);
  private jobsSub: Subscription | null = null;
  private statusPollSub: Subscription | null = null;

  loading = false;
  error: string | null = null;
  researchReviewResult: ResearchAndReviewResponse | null = null;
  fullPipelineResult: FullPipelineResponse | null = null;

  allJobs: BlogJobListItem[] = [];
  runningJobs: BlogJobListItem[] = [];
  completedJobs: BlogJobListItem[] = [];
  selectedBlogJob: BlogJobListItem | null = null;
  selectedJobStatus: BlogJobStatusResponse | null = null;
  selectedJobArtifacts: string[] = [];
  artifactsLoading = false;
  artifactsError: string | null = null;
  artifactContent: Record<string, string | object> = {};
  artifactContentLoading: Record<string, boolean> = {};

  isTerminalStatus(status: string): boolean {
    return (TERMINAL_STATUSES as readonly string[]).includes(status);
  }

  ngOnInit(): void {
    this.jobsSub = timer(0, POLL_JOBS_MS).pipe(
      switchMap(() => this.api.getJobs(false))
    ).subscribe({
      next: (jobs) => {
        this.allJobs = jobs;
        this.runningJobs = jobs.filter((j) => j.status === 'pending' || j.status === 'running');
        this.completedJobs = jobs.filter((j) => this.isTerminalStatus(j.status));
        if (this.selectedBlogJob) {
          const still = jobs.find((j) => j.job_id === this.selectedBlogJob!.job_id);
          if (!still) this.clearSelection();
        } else if (this.runningJobs.length > 0) {
          this.selectJob(this.runningJobs[0]);
        } else if (this.completedJobs.length > 0) {
          this.selectJob(this.completedJobs[0]);
        }
      },
    });
  }

  ngOnDestroy(): void {
    this.jobsSub?.unsubscribe();
    this.statusPollSub?.unsubscribe();
  }

  private clearSelection(): void {
    this.selectedBlogJob = null;
    this.selectedJobStatus = null;
    this.statusPollSub?.unsubscribe();
    this.statusPollSub = null;
    this.selectedJobArtifacts = [];
    this.artifactContent = {};
    this.artifactContentLoading = {};
    this.artifactsError = null;
  }

  selectJob(job: BlogJobListItem): void {
    this.selectedBlogJob = job;
    this.selectedJobArtifacts = [];
    this.artifactContent = {};
    this.artifactContentLoading = {};
    this.artifactsError = null;
    this.statusPollSub?.unsubscribe();
    this.statusPollSub = timer(0, POLL_STATUS_MS).pipe(
      switchMap(() => this.api.getJobStatus(job.job_id))
    ).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        if (this.isTerminalStatus(status.status) && this.selectedBlogJob?.job_id === job.job_id) {
          this.loadArtifactsList(job.job_id);
        }
      },
    });
    if (this.isTerminalStatus(job.status)) this.loadArtifactsList(job.job_id);
  }

  private loadArtifactsList(jobId: string): void {
    if (this.selectedBlogJob?.job_id !== jobId) return;
    this.artifactsLoading = true;
    this.artifactsError = null;
    this.api.getJobArtifacts(jobId).subscribe({
      next: (res) => {
        if (this.selectedBlogJob?.job_id === jobId) {
          this.selectedJobArtifacts = res.artifacts ?? [];
          this.artifactsLoading = false;
        }
      },
      error: (err) => {
        if (this.selectedBlogJob?.job_id === jobId) {
          this.artifactsError = err?.error?.detail ?? err?.message ?? 'Failed to load artifacts';
          this.selectedJobArtifacts = [];
          this.artifactsLoading = false;
        }
      },
    });
  }

  loadArtifactContent(artifactName: string): void {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId || this.artifactContent[artifactName] !== undefined) return;
    this.artifactContentLoading[artifactName] = true;
    this.api.getJobArtifactContent(jobId, artifactName).subscribe({
      next: (res) => {
        this.artifactContent[artifactName] = res.content;
        this.artifactContentLoading[artifactName] = false;
      },
      error: () => {
        this.artifactContentLoading[artifactName] = false;
      },
    });
  }

  getArtifactContentDisplay(name: string): string {
    const content = this.artifactContent[name];
    if (content === undefined) return '';
    if (typeof content === 'string') return content;
    return JSON.stringify(content, null, 2);
  }

  isArtifactJson(name: string): boolean {
    return name.endsWith('.json');
  }

  onResearchReviewSubmit(request: ResearchAndReviewRequest): void {
    this.loading = true;
    this.error = null;
    this.researchReviewResult = null;
    this.api.startResearchReviewAsync(request).subscribe({
      next: (res) => {
        this.loading = false;
        this.api.getJobs(false).subscribe((jobs) => {
          this.allJobs = jobs;
          this.runningJobs = jobs.filter((j) => j.status === 'pending' || j.status === 'running');
          this.completedJobs = jobs.filter((j) => this.isTerminalStatus(j.status));
          const j = jobs.find((x) => x.job_id === res.job_id);
          if (j) this.selectJob(j);
          else {
            const newJob: BlogJobListItem = {
              job_id: res.job_id,
              status: 'running',
              brief: request.brief.slice(0, 100),
              progress: 0,
            };
            this.allJobs = [newJob, ...this.allJobs];
            this.runningJobs = [newJob, ...this.runningJobs];
            this.selectJob(newJob);
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
        this.api.getJobs(false).subscribe((jobs) => {
          this.allJobs = jobs;
          this.runningJobs = jobs.filter((j) => j.status === 'pending' || j.status === 'running');
          this.completedJobs = jobs.filter((j) => this.isTerminalStatus(j.status));
          const j = jobs.find((x) => x.job_id === res.job_id);
          if (j) this.selectJob(j);
          else {
            const newJob: BlogJobListItem = {
              job_id: res.job_id,
              status: 'running',
              brief: request.brief.slice(0, 100),
              progress: 0,
            };
            this.allJobs = [newJob, ...this.allJobs];
            this.runningJobs = [newJob, ...this.runningJobs];
            this.selectJob(newJob);
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
