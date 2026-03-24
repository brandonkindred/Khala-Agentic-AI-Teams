import { Component, inject, OnDestroy, OnInit } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { MatTabsModule } from '@angular/material/tabs';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { CommonModule } from '@angular/common';
import { Subscription, timer, forkJoin, of } from 'rxjs';
import { switchMap, map, catchError } from 'rxjs/operators';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { PlanningV3ApiService } from '../../services/planning-v3-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { RunTeamFormComponent } from '../run-team-form/run-team-form.component';
import { RetryFailedComponent } from '../retry-failed/retry-failed.component';
import { ArchitectureResultsComponent } from '../architecture-results/architecture-results.component';
import { BackendCodeV2RunFormComponent } from '../backend-code-v2-run-form/backend-code-v2-run-form.component';
import { BackendCodeV2JobStatusComponent } from '../backend-code-v2-job-status/backend-code-v2-job-status.component';
import { FrontendCodeV2RunFormComponent } from '../frontend-code-v2-run-form/frontend-code-v2-run-form.component';
import { FrontendCodeV2JobStatusComponent } from '../frontend-code-v2-job-status/frontend-code-v2-job-status.component';
import { PlanningV3RunFormComponent } from '../planning-v3-run-form/planning-v3-run-form.component';
import { PlanningV3JobStatusComponent } from '../planning-v3-job-status/planning-v3-job-status.component';
import { RunTeamTrackingComponent } from '../run-team-tracking/run-team-tracking.component';
import { PendingQuestionsComponent } from '../pending-questions/pending-questions.component';
import { ProductAnalysisRunFormComponent } from '../product-analysis-run-form/product-analysis-run-form.component';
import { ProductAnalysisJobStatusComponent } from '../product-analysis-job-status/product-analysis-job-status.component';
import { StartFromSpecFormComponent } from '../start-from-spec-form/start-from-spec-form.component';
import type {
  RunTeamResponse,
  JobStatusResponse,
  ArchitectDesignResponse,
  BackendCodeV2RunRequest,
  FrontendCodeV2RunRequest,
  PlanningV2RunRequest,
  PlanningV2StatusResponse,
  PlanningV3RunRequest,
  PlanningV3StatusResponse,
  ProductAnalysisRunRequest,
  ProductAnalysisStatusResponse,
  RunningJobSummary,
} from '../../models';

@Component({
  selector: 'app-software-engineering-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatTabsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatTooltipModule,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    HealthIndicatorComponent,
    RunTeamFormComponent,
    RetryFailedComponent,
    ArchitectureResultsComponent,
    BackendCodeV2RunFormComponent,
    BackendCodeV2JobStatusComponent,
    FrontendCodeV2RunFormComponent,
    FrontendCodeV2JobStatusComponent,
    PlanningV3RunFormComponent,
    PlanningV3JobStatusComponent,
    RunTeamTrackingComponent,
    PendingQuestionsComponent,
    ProductAnalysisRunFormComponent,
    ProductAnalysisJobStatusComponent,
    StartFromSpecFormComponent,
  ],
  templateUrl: './software-engineering-dashboard.component.html',
  styleUrl: './software-engineering-dashboard.component.scss',
})
export class SoftwareEngineeringDashboardComponent implements OnInit, OnDestroy {
  private static readonly JOB_TYPE_TAB_MAP: Record<string, number> = {
    run_team: 0,
    product_analysis: 1,
    backend_code_v2: 2,
    frontend_code_v2: 3,
    planning_v3: 4,
  };
  private readonly api = inject(SoftwareEngineeringApiService);
  private readonly planningV3Api = inject(PlanningV3ApiService);
  private readonly route = inject(ActivatedRoute);
  private runningJobsSub: Subscription | null = null;
  private pendingJobId: string | null = null;
  private pendingTabIndex: number | null = null;

  loading = false;
  error: string | null = null;
  jobId: string | null = null;
  selectedTabIndex = 0;
  jobStatus: JobStatusResponse | null = null;
  architectSpec = '';
  architectResults: ArchitectDesignResponse | null = null;
  backendCodeV2JobId: string | null = null;
  frontendCodeV2JobId: string | null = null;
  planningV2JobId: string | null = null;
  planningV3JobId: string | null = null;
  productAnalysisJobId: string | null = null;

  /** Running jobs from GET /run-team/jobs; used by the right-hand panel. */
  runningJobs: RunningJobSummary[] = [];
  /** Error from failed getRunningJobs() so user sees why the list is empty. */
  runningJobsError: string | null = null;
  /** Job selected in the running-jobs panel for monitoring. */
  selectedRunningJob: RunningJobSummary | null = null;
  /** Status for the selected run_team job in the panel (from JobStatusComponent). */
  panelRunTeamStatus: JobStatusResponse | null = null;
  /** Status for planning-v3 job in the main tab. */
  planningV3Status: PlanningV3StatusResponse | null = null;
  /** Status for product-analysis job in the main tab. */
  productAnalysisStatus: ProductAnalysisStatusResponse | null = null;
  /** Status for the selected planning_v3 job in the panel. */
  panelPlanningV3Status: PlanningV3StatusResponse | null = null;

  healthCheck = (): ReturnType<SoftwareEngineeringApiService['health']> =>
    this.api.health();

  /** True when the run_team job can be resumed (e.g. after server restart). */
  isRunTeamJobResumable(): boolean {
    const s = this.jobStatus?.status;
    return s === 'failed' || s === 'cancelled' || s === 'agent_crash';
  }

  ngOnInit(): void {
    this.route.queryParams.subscribe((params) => {
      const jobId = params['jobId'];
      const tab = params['tab'];
      if (jobId) {
        this.pendingJobId = jobId;
      }
      if (tab !== undefined) {
        this.pendingTabIndex = parseInt(tab, 10);
      }
    });

    this.runningJobsSub = timer(0, 30000).pipe(
      switchMap(() =>
        forkJoin({
          se: this.api.getRunningJobs(false),
          planningV3: this.planningV3Api.getJobs().pipe(
            map((r) => r.jobs.map((j) => ({ job_id: j.job_id, status: j.status, repo_path: j.repo_path, job_type: 'planning_v3' as const }))),
            catchError(() => of([] as RunningJobSummary[]))
          ),
        }).pipe(
          map(({ se, planningV3 }) => ({ jobs: [...se.jobs, ...planningV3] }))
        )
      )
    ).subscribe({
      next: (res) => {
        this.runningJobsError = null;
        this.runningJobs = res.jobs;
        if (this.runningJobs.length === 0) {
          this.selectedRunningJob = null;
          this.panelRunTeamStatus = null;
          this.panelPlanningV3Status = null;
        } else if (this.selectedRunningJob && !this.runningJobs.find(j => j.job_id === this.selectedRunningJob!.job_id)) {
          this.selectedRunningJob = null;
          this.panelRunTeamStatus = null;
          this.panelPlanningV3Status = null;
        }

        if (this.pendingJobId) {
          const job = this.runningJobs.find(j => j.job_id === this.pendingJobId);
          if (job) {
            this.selectRunningJob(job);
            this.pendingJobId = null;
            this.pendingTabIndex = null;
          } else {
            this.selectJobById(this.pendingJobId, this.pendingTabIndex);
            this.pendingJobId = null;
            this.pendingTabIndex = null;
          }
        } else if (this.runningJobs.length > 0 && !this.selectedRunningJob) {
          this.selectRunningJob(this.runningJobs[0]);
        }
      },
      error: (err) => {
        this.runningJobsError = err?.error?.detail ?? err?.message ?? 'Failed to load jobs list';
        this.runningJobs = [];
      },
    });
  }

  ngOnDestroy(): void {
    this.runningJobsSub?.unsubscribe();
  }

  selectRunningJob(job: RunningJobSummary): void {
    this.selectedRunningJob = job;
    if (job.job_type !== 'run_team') {
      this.panelRunTeamStatus = null;
    }
    if (job.job_type !== 'planning_v3') {
      this.panelPlanningV3Status = null;
    }

    const tabIndex = SoftwareEngineeringDashboardComponent.JOB_TYPE_TAB_MAP[job.job_type];
    if (tabIndex !== undefined) {
      this.selectedTabIndex = tabIndex;
    }

    switch (job.job_type) {
      case 'run_team':
        this.jobId = job.job_id;
        break;
      case 'product_analysis':
        this.productAnalysisJobId = job.job_id;
        break;
      case 'backend_code_v2':
        this.backendCodeV2JobId = job.job_id;
        break;
      case 'frontend_code_v2':
        this.frontendCodeV2JobId = job.job_id;
        break;
      case 'planning_v3':
        this.planningV3JobId = job.job_id;
        break;
    }
  }

  selectJobById(jobId: string, tabIndex?: number | null): void {
    if (tabIndex !== undefined && tabIndex !== null) {
      this.selectedTabIndex = tabIndex;
    }

    switch (tabIndex) {
      case 0:
        this.jobId = jobId;
        break;
      case 1:
        this.productAnalysisJobId = jobId;
        break;
      case 2:
        this.backendCodeV2JobId = jobId;
        break;
      case 3:
        this.frontendCodeV2JobId = jobId;
        break;
      case 4:
        this.planningV3JobId = jobId;
        break;
      default:
        this.jobId = jobId;
        break;
    }
  }

  runningJobTypeLabel(jobType: string): string {
    const labels: Record<string, string> = {
      run_team: 'Run Team',
      product_analysis: 'Product Analysis',
      backend_code_v2: 'Backend (v2)',
      frontend_code_v2: 'Frontend (v2)',
      planning_v2: 'Planning (v2)',
      planning_v3: 'Planning',
    };
    return labels[jobType] ?? jobType;
  }

  onPlanningV2Submit(req: PlanningV2RunRequest): void {
    this.loading = true;
    this.error = null;
    this.api.runPlanningV2(req).subscribe({
      next: (res) => {
        this.planningV2JobId = res.job_id;
        this.loading = false;
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to run planning v2';
        this.loading = false;
      },
    });
  }

  onRunTeamSubmit(response: RunTeamResponse): void {
    this.jobId = response.job_id;
  }

  onRetryFailed(): void {
    if (!this.jobId) return;
    this.loading = true;
    this.error = null;
    this.api.retryFailed(this.jobId).subscribe({
      next: () => {
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Retry failed';
        this.loading = false;
      },
    });
  }

  onResumeRunTeamJob(): void {
    if (!this.jobId) return;
    this.loading = true;
    this.error = null;
    this.api.resumeRunTeamJob(this.jobId).subscribe({
      next: () => {
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Resume failed';
        this.loading = false;
      },
    });
  }

  clearRunTeamJob(): void {
    this.jobId = null;
    this.jobStatus = null;
    this.selectedRunningJob = null;
  }

  onDeleteRunTeamJob(): void {
    if (!this.jobId) return;
    if (!confirm('Permanently delete this job? It will be removed from the list.')) {
      return;
    }
    this.api.deleteJob(this.jobId).subscribe({
      next: () => this.clearRunTeamJob(),
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to delete job';
      },
    });
  }

  onBackendCodeV2Submit(request: BackendCodeV2RunRequest): void {
    this.loading = true;
    this.error = null;
    this.backendCodeV2JobId = null;
    this.api.runBackendCodeV2(request).subscribe({
      next: (res) => {
        this.backendCodeV2JobId = res.job_id;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Backend-Code-V2 request failed';
        this.loading = false;
      },
    });
  }

  onFrontendCodeV2Submit(request: FrontendCodeV2RunRequest): void {
    this.loading = true;
    this.error = null;
    this.frontendCodeV2JobId = null;
    this.api.runFrontendCodeV2(request).subscribe({
      next: (res) => {
        this.frontendCodeV2JobId = res.job_id;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Frontend-Code-V2 request failed';
        this.loading = false;
      },
    });
  }

  onPlanningV3Submit(request: PlanningV3RunRequest): void {
    this.loading = true;
    this.error = null;
    this.planningV3JobId = null;
    this.planningV3Api.run(request).subscribe({
      next: (res) => {
        this.planningV3JobId = res.job_id;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Planning request failed';
        this.loading = false;
      },
    });
  }

  onArchitectDesignSubmit(): void {
    if (!this.architectSpec?.trim()) return;
    this.loading = true;
    this.error = null;
    this.architectResults = null;
    this.api.architectDesign({ spec: this.architectSpec.trim() }).subscribe({
      next: (res) => {
        this.architectResults = res;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Architecture design failed';
        this.loading = false;
      },
    });
  }

  onAnswersSubmitted(
    response: JobStatusResponse | PlanningV2StatusResponse | PlanningV3StatusResponse | ProductAnalysisStatusResponse,
  ): void {
    this.jobStatus = response as JobStatusResponse;
  }

  onPlanningV3StatusChange(status: PlanningV3StatusResponse): void {
    this.planningV3Status = status;
  }

  onPlanningV3AnswersSubmitted(
    response: JobStatusResponse | PlanningV2StatusResponse | PlanningV3StatusResponse | ProductAnalysisStatusResponse,
  ): void {
    this.planningV3Status = response as PlanningV3StatusResponse;
  }

  onPanelRunTeamAnswersSubmitted(response: JobStatusResponse | PlanningV3StatusResponse | ProductAnalysisStatusResponse): void {
    this.panelRunTeamStatus = response as JobStatusResponse;
  }

  onPanelPlanningV3StatusChange(status: PlanningV3StatusResponse): void {
    this.panelPlanningV3Status = status;
  }

  onPanelPlanningV3AnswersSubmitted(response: JobStatusResponse | PlanningV3StatusResponse | ProductAnalysisStatusResponse): void {
    this.panelPlanningV3Status = response as PlanningV3StatusResponse;
  }

  onStartFromSpecSuccess(res: { job_id: string }): void {
    this.error = null;
    this.productAnalysisJobId = res.job_id;
  }


  onStartFromSpecError(message: string): void {
    this.error = message || 'Failed to create project and start analysis.';
  }

  onProductAnalysisSubmit(request: ProductAnalysisRunRequest): void {
    this.loading = true;
    this.error = null;
    this.productAnalysisJobId = null;
    this.api.runProductAnalysis(request).subscribe({
      next: (res) => {
        this.productAnalysisJobId = res.job_id;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Product Analysis request failed';
        this.loading = false;
      },
    });
  }

  onProductAnalysisStatusChange(status: ProductAnalysisStatusResponse): void {
    this.productAnalysisStatus = status;
  }

  onProductAnalysisAnswersSubmitted(
    response: JobStatusResponse | PlanningV2StatusResponse | PlanningV3StatusResponse | ProductAnalysisStatusResponse,
  ): void {
    this.productAnalysisStatus = response as ProductAnalysisStatusResponse;
  }
}
