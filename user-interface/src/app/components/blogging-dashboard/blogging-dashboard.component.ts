import { Component, inject, OnDestroy, OnInit } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { CommonModule, SlicePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { marked } from 'marked';
import { MatTabsModule } from '@angular/material/tabs';
import { MatCardModule } from '@angular/material/card';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { BloggingApiService } from '../../services/blogging-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { ResearchReviewFormComponent } from '../research-review-form/research-review-form.component';
import { ResearchReviewResultsComponent } from '../research-review-results/research-review-results.component';
import { FullPipelineFormComponent } from '../full-pipeline-form/full-pipeline-form.component';
import { FullPipelineResultsComponent } from '../full-pipeline-results/full-pipeline-results.component';
import { MediumStatsFormComponent } from '../medium-stats-form/medium-stats-form.component';
import { BlogPipelineFlowComponent } from '../blog-pipeline-flow/blog-pipeline-flow.component';
import { Router } from '@angular/router';
import type {
  ResearchAndReviewRequest,
  ResearchAndReviewResponse,
  FullPipelineRequest,
  FullPipelineResponse,
  MediumStatsRequest,
  BlogJobListItem,
  BlogJobStatusResponse,
  ArtifactMeta,
} from '../../models';

/**
 * Blogging API dashboard: research-and-review and full-pipeline forms and results.
 * Shows Jobs panel (running and completed) with job details and produced assets.
 */
const TERMINAL_STATUSES = ['completed', 'needs_human_review', 'failed'] as const;
const POLL_JOBS_MS = 12000;
const POLL_STATUS_MS = 2000; // Poll selected job status every 2s for frequent status updates

export function artifactLabel(name: string): string {
  const labels: Record<string, string> = {
    'brand_spec_prompt.md': 'Brand spec prompt',
    'content_brief.md': 'Content brief',
    'content_plan.json': 'Content plan (JSON)',
    'content_plan.md': 'Content plan',
    'research_packet.md': 'Research packet',
    'allowed_claims.json': 'Allowed claims',
    'outline.md': 'Outline',
    'draft_v1.md': 'Draft v1',
    'draft_v2.md': 'Draft v2',
    'final.md': 'Final draft',
    'compliance_report.json': 'Compliance report',
    'fact_check_report.json': 'Fact check report',
    'validator_report.json': 'Validator report',
    'publishing_pack.json': 'Publishing pack',
    'medium_stats_report.json': 'Medium statistics report',
  };
  return labels[name] ?? name;
}

@Component({
  selector: 'app-blogging-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    SlicePipe,
    FormsModule,
    MatTabsModule,
    MatCardModule,
    MatExpansionModule,
    MatButtonModule,
    MatIconModule,
    MatTooltipModule,
    MatProgressBarModule,
    MatFormFieldModule,
    MatInputModule,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    ResearchReviewFormComponent,
    ResearchReviewResultsComponent,
    FullPipelineFormComponent,
    FullPipelineResultsComponent,
    MediumStatsFormComponent,
    BlogPipelineFlowComponent,
  ],
  templateUrl: './blogging-dashboard.component.html',
  styleUrl: './blogging-dashboard.component.scss',
})
export class BloggingDashboardComponent implements OnInit, OnDestroy {
  readonly artifactLabel = artifactLabel;
  private readonly api = inject(BloggingApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly sanitizer = inject(DomSanitizer);
  private jobsSub: Subscription | null = null;
  private statusPollSub: Subscription | null = null;
  private queryParamsSub: Subscription | null = null;
  private pendingJobId: string | null = null;

  loading = false;
  error: string | null = null;
  researchReviewResult: ResearchAndReviewResponse | null = null;
  fullPipelineResult: FullPipelineResponse | null = null;

  allJobs: BlogJobListItem[] = [];
  runningJobs: BlogJobListItem[] = [];
  completedJobs: BlogJobListItem[] = [];
  selectedBlogJob: BlogJobListItem | null = null;
  selectedJobStatus: BlogJobStatusResponse | null = null;
  selectedJobArtifacts: ArtifactMeta[] = [];
  artifactsLoading = false;
  artifactsError: string | null = null;
  artifactContent: Record<string, string | object> = {};
  artifactContentLoading: Record<string, boolean> = {};
  activeTabIndex = 1; // 0 Research and Review, 1 Full Pipeline, 2 Assets — default to Full Pipeline
  /** From GET /health when `brand_spec_configured` is true — hides audience/tone on full pipeline form. */
  blogBrandSpecConfigured = false;
  viewArtifactModal: { name: string; content: string | object } | null = null;
  viewArtifactLoading = false;
  viewArtifactError: string | null = null;

  // Collaboration: title selection
  collaborationError: string | null = null;

  // Collaboration: story elicitation
  storyResponseText = '';
  storySubmitting = false;

  // Collaboration: Q&A
  qaAnswers: Record<string, string> = {};
  qaSubmitting = false;

  isTerminalStatus(status: string): boolean {
    return (TERMINAL_STATUSES as readonly string[]).includes(status);
  }

  canStopSelectedJob(): boolean {
    const status = this.selectedJobStatus?.status ?? this.selectedBlogJob?.status;
    return !!this.selectedBlogJob && !!status && !this.isTerminalStatus(status) && status !== 'cancelled';
  }

  cancelSelectedJob(): void {
    if (!this.selectedBlogJob) return;
    const jobId = this.selectedBlogJob.job_id;
    this.api.cancelJob(jobId).subscribe({
      next: () => {
        this.api.getJobStatus(jobId).subscribe({
          next: (status) => {
            this.selectedJobStatus = status;
          },
        });
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to cancel job';
      },
    });
  }

  deleteSelectedJob(): void {
    if (!this.selectedBlogJob) return;
    if (!confirm('Delete this job? This cannot be undone.')) return;
    const jobId = this.selectedBlogJob.job_id;
    this.api.deleteJob(jobId).subscribe({
      next: () => {
        this.clearSelection();
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to delete job';
      },
    });
  }

  ngOnInit(): void {
    this.api.health().subscribe({
      next: (h) => {
        this.blogBrandSpecConfigured = h.brand_spec_configured === true;
      },
      error: () => {
        this.blogBrandSpecConfigured = false;
      },
    });

    this.queryParamsSub = this.route.queryParams.subscribe((params) => {
      const id = params['jobId'];
      if (id) this.pendingJobId = id;
    });
    this.jobsSub = timer(0, POLL_JOBS_MS).pipe(
      switchMap(() => this.api.getJobs(false))
    ).subscribe({
      next: (jobs) => {
        this.allJobs = jobs;
        this.runningJobs = jobs.filter((j) => j.status === 'pending' || j.status === 'running');
        this.completedJobs = jobs.filter((j) => this.isTerminalStatus(j.status));
        if (this.pendingJobId != null) {
          const job = jobs.find((j) => j.job_id === this.pendingJobId);
          if (job) this.selectJob(job);
          this.pendingJobId = null;
        } else if (this.selectedBlogJob) {
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
    this.queryParamsSub?.unsubscribe();
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
    this.collaborationError = null;
    this.storyResponseText = '';
    this.qaAnswers = {};
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

  isArtifactMarkdown(name: string): boolean {
    return name.endsWith('.md');
  }

  isArtifactYaml(name: string): boolean {
    return name.endsWith('.yaml') || name.endsWith('.yml');
  }

  openAssetInNewTab(artifactName: string): void {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId) return;
    const url = this.router.serializeUrl(
      this.router.createUrlTree(['/blogging/jobs', jobId, 'artifacts', artifactName])
    );
    window.open(url, '_blank', 'noopener');
  }

  getArtifactDownloadUrl(artifactName: string): string {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId) return '#';
    return this.api.getJobArtifactDownloadUrl(jobId, artifactName);
  }

  openViewModal(artifactName: string): void {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId) return;
    this.viewArtifactModal = null;
    this.viewArtifactError = null;
    this.viewArtifactLoading = true;
    this.api.getJobArtifactContent(jobId, artifactName).subscribe({
      next: (res) => {
        this.viewArtifactModal = { name: res.name, content: res.content };
        this.viewArtifactLoading = false;
      },
      error: (err) => {
        this.viewArtifactError = err?.error?.detail ?? err?.message ?? 'Failed to load artifact';
        this.viewArtifactLoading = false;
      },
    });
  }

  closeViewModal(): void {
    this.viewArtifactModal = null;
    this.viewArtifactError = null;
  }

  getViewModalDisplayContent(): string {
    if (!this.viewArtifactModal) return '';
    const content = this.viewArtifactModal.content;
    if (typeof content === 'string') return content;
    return JSON.stringify(content, null, 2);
  }

  getViewModalMarkdownHtml(): SafeHtml {
    if (!this.viewArtifactModal || !this.isArtifactMarkdown(this.viewArtifactModal.name)) return '';
    const content = this.viewArtifactModal.content;
    const text = typeof content === 'string' ? content : JSON.stringify(content, null, 2);
    if (!text?.trim()) return this.sanitizer.bypassSecurityTrustHtml('');
    try {
      const result = marked.parse(text);
      const html = typeof result === 'string' ? result : '';
      return this.sanitizer.bypassSecurityTrustHtml(html || `<pre>${this.escapeHtml(text)}</pre>`);
    } catch {
      return this.sanitizer.bypassSecurityTrustHtml(`<pre>${this.escapeHtml(text)}</pre>`);
    }
  }

  private escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  jobApprovedLabel(): string {
    if (!this.selectedJobStatus?.approved_at) return 'No';
    return 'Yes';
  }

  canApproveJob(): boolean {
    const status = this.selectedJobStatus?.status ?? this.selectedBlogJob?.status;
    return status === 'completed' || status === 'needs_human_review';
  }

  canUnapproveJob(): boolean {
    return !!this.selectedJobStatus?.approved_at;
  }

  approveSelectedJob(): void {
    if (!this.selectedBlogJob) return;
    this.api.approveJob(this.selectedBlogJob.job_id).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        this.error = null;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to approve job';
      },
    });
  }

  unapproveSelectedJob(): void {
    if (!this.selectedBlogJob) return;
    this.api.unapproveJob(this.selectedBlogJob.job_id).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        this.error = null;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to unapprove job';
      },
    });
  }

  // ---------------------------------------------------------------------------
  // Collaboration: title selection
  // ---------------------------------------------------------------------------

  selectTitle(title: string): void {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId) return;
    this.collaborationError = null;
    this.api.selectTitle(jobId, title).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
      },
      error: (err) => {
        this.collaborationError = err?.error?.detail ?? err?.message ?? 'Failed to submit title selection';
      },
    });
  }

  // ---------------------------------------------------------------------------
  // Collaboration: story elicitation
  // ---------------------------------------------------------------------------

  submitStoryResponse(): void {
    const jobId = this.selectedBlogJob?.job_id;
    const msg = this.storyResponseText.trim();
    if (!jobId || !msg) return;
    this.storySubmitting = true;
    this.collaborationError = null;
    this.api.submitStoryResponse(jobId, msg).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        this.storyResponseText = '';
        this.storySubmitting = false;
      },
      error: (err) => {
        this.collaborationError = err?.error?.detail ?? err?.message ?? 'Failed to submit story response';
        this.storySubmitting = false;
      },
    });
  }

  skipStoryGap(): void {
    const jobId = this.selectedBlogJob?.job_id;
    if (!jobId) return;
    this.collaborationError = null;
    this.api.skipStoryGap(jobId).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        this.storyResponseText = '';
      },
      error: (err) => {
        this.collaborationError = err?.error?.detail ?? err?.message ?? 'Failed to skip story gap';
      },
    });
  }

  getCurrentStoryGap() {
    const status = this.selectedJobStatus;
    if (!status?.story_gaps?.length) return null;
    const idx = status.current_story_gap_index ?? 0;
    return status.story_gaps[idx] ?? null;
  }

  getStoryAgentMessages() {
    const status = this.selectedJobStatus;
    const idx = status?.current_story_gap_index ?? 0;
    return (status?.story_chat_history ?? []).filter(
      (m) => m.gap_index === idx || m.gap_index === undefined
    );
  }

  // ---------------------------------------------------------------------------
  // Collaboration: Q&A
  // ---------------------------------------------------------------------------

  submitQaAnswers(): void {
    const jobId = this.selectedBlogJob?.job_id;
    const questions = this.selectedJobStatus?.pending_questions ?? [];
    if (!jobId || !questions.length) return;
    this.qaSubmitting = true;
    this.collaborationError = null;
    const answers = questions.map((q) => ({
      question_id: q.id,
      selected_answer: this.qaAnswers[q.id] ?? '',
    }));
    this.api.submitBlogAnswers(jobId, answers).subscribe({
      next: (status) => {
        this.selectedJobStatus = status;
        this.qaAnswers = {};
        this.qaSubmitting = false;
      },
      error: (err) => {
        this.collaborationError = err?.error?.detail ?? err?.message ?? 'Failed to submit answers';
        this.qaSubmitting = false;
      },
    });
  }

  allQaAnswered(): boolean {
    const questions = this.selectedJobStatus?.pending_questions ?? [];
    return questions.every((q) => !q.required || (this.qaAnswers[q.id] ?? '').trim().length > 0);
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

  onMediumStatsSubmit(request: MediumStatsRequest): void {
    this.loading = true;
    this.error = null;
    this.api.startMediumStatsAsync(request).subscribe({
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
              brief: 'Medium post statistics',
              progress: 0,
              job_type: 'medium_stats',
            };
            this.allJobs = [newJob, ...this.allJobs];
            this.runningJobs = [newJob, ...this.runningJobs];
            this.selectJob(newJob);
          }
        });
        this.router.navigate(['/blogging'], { queryParams: { jobId: res.job_id } });
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to start Medium stats job';
        this.loading = false;
      },
    });
  }
}
