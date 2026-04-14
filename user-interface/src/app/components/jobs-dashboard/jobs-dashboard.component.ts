import { Component, OnInit, OnDestroy, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatTooltipModule } from '@angular/material/tooltip';
import { Subscription, forkJoin, of, timer } from 'rxjs';
import { switchMap, catchError, map } from 'rxjs/operators';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { BloggingApiService } from '../../services/blogging-api.service';
import { AISystemsApiService } from '../../services/ai-systems-api.service';
import { AgentProvisioningApiService } from '../../services/agent-provisioning-api.service';
import { SocialMarketingApiService } from '../../services/social-marketing-api.service';
import { InvestmentApiService } from '../../services/investment-api.service';
import { PersonaTestingApiService } from '../../services/persona-testing-api.service';
import { SalesApiService } from '../../services/sales-api.service';
import { PlanningV3ApiService } from '../../services/planning-v3-api.service';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { GenericJobsApiService } from '../../services/generic-jobs-api.service';
import { JobActionsService } from '../../services/job-actions.service';
import type {
  RunningJobSummary,
  JobStatusResponse,
  PlanningV2StatusResponse,
  ProductAnalysisStatusResponse,
  TeamProgressEntry,
} from '../../models';
import {
  type DashboardRow,
  type SEDetail,
  type TeamStatus,
  SOURCE_DISPLAY,
  fromRunningJobSummary,
  fromBlogJobListItem,
  fromAISystemJobSummary,
  fromProvisionJobSummary,
  fromSocialMarketingJobListItem,
  fromInvestmentJobSummary,
  fromFounderJobSummary,
  fromSalesJobListItem,
  fromPlanningV3JobSummary,
  fromGenericJobRecord,
} from '../../models';

/** Job type metadata for SE display. */
interface JobTypeInfo {
  label: string;
  icon: string;
  route: string;
  tabIndex?: number;
}

const JOB_TYPE_INFO: Record<string, JobTypeInfo> = {
  'run_team': { label: 'Run Team', icon: 'groups', route: '/software-engineering', tabIndex: 0 },
  'planning_v3': { label: 'Planning', icon: 'description', route: '/software-engineering/planning-v3' },
  'backend_code_v2': { label: 'Backend Code V2', icon: 'dns', route: '/software-engineering', tabIndex: 2 },
  'frontend_code_v2': { label: 'Frontend Code V2', icon: 'web', route: '/software-engineering', tabIndex: 3 },
  'product_analysis': { label: 'Product Analysis', icon: 'analytics', route: '/software-engineering', tabIndex: 1 },
};

const TEAM_DISPLAY_INFO: Record<string, { label: string; icon: string }> = {
  'planning': { label: 'Planning', icon: 'architecture' },
  'backend-code-v2': { label: 'Backend', icon: 'dns' },
  'frontend-code-v2': { label: 'Frontend', icon: 'web' },
  'backend': { label: 'Backend', icon: 'dns' },
  'frontend': { label: 'Frontend', icon: 'web' },
  'devops': { label: 'DevOps', icon: 'build' },
  'product_analysis': { label: 'Analysis', icon: 'analytics' },
};

const PHASE_DISPLAY: Record<string, string> = {
  'setup': 'Setup',
  'planning': 'Planning',
  'execution': 'Execution',
  'review': 'Review',
  'documentation': 'Docs',
  'deliver': 'Deliver',
  'completed': 'Done',
  'coding': 'Coding',
  'code_review': 'Code Review',
  'qa_testing': 'QA',
  'security_testing': 'Security',
  'problem_solving': 'Fixing',
};

@Component({
  selector: 'app-jobs-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatProgressBarModule,
    MatIconModule,
    MatButtonModule,
    MatTooltipModule,
  ],
  templateUrl: './jobs-dashboard.component.html',
  styleUrl: './jobs-dashboard.component.scss',
})
export class JobsDashboardComponent implements OnInit, OnDestroy {
  private readonly seApi = inject(SoftwareEngineeringApiService);
  private readonly bloggingApi = inject(BloggingApiService);
  private readonly aiSystemsApi = inject(AISystemsApiService);
  private readonly agentProvisioningApi = inject(AgentProvisioningApiService);
  private readonly socialMarketingApi = inject(SocialMarketingApiService);
  private readonly investmentApi = inject(InvestmentApiService);
  private readonly personaApi = inject(PersonaTestingApiService);
  private readonly salesApi = inject(SalesApiService);
  private readonly planningV3Api = inject(PlanningV3ApiService);
  private readonly codingTeamApi = inject(CodingTeamApiService);
  private readonly genericJobsApi = inject(GenericJobsApiService);
  private readonly jobActions = inject(JobActionsService);
  private readonly router = inject(Router);

  jobs: DashboardRow[] = [];
  loading = true;
  error: string | null = null;
  lastUpdated: Date | null = null;
  /** Set when GET /run-team/jobs fails so user sees why SE jobs are missing. */
  seFetchError: string | null = null;

  readonly SOURCE_DISPLAY = SOURCE_DISPLAY;

  private pollSub: Subscription | null = null;
  private readonly POLL_INTERVAL = 20000;

  ngOnInit(): void {
    this.startPolling();
  }

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
  }

  private startPolling(): void {
    this.pollSub?.unsubscribe();
    this.pollSub = timer(0, this.POLL_INTERVAL)
      .pipe(
        switchMap(() => this.fetchAllJobLists()),
        switchMap((rows) => this.enrichSERows(rows)),
        catchError((err) => {
          this.error = err?.message ?? 'Failed to fetch jobs';
          this.loading = false;
          return of([]);
        })
      )
      .subscribe({
        next: (dashboardRows) => {
          this.jobs = dashboardRows;
          this.loading = false;
          this.error = null;
          this.lastUpdated = new Date();
        },
      });
  }

  /** Fetch from all team list endpoints and merge into sorted DashboardRow[] (seDetail not set yet). */
  private fetchAllJobLists() {
    return forkJoin({
      se: this.seApi.getRunningJobs(false).pipe(
        catchError((err) =>
          of({
            jobs: [] as RunningJobSummary[],
            _error: err?.message ?? err?.error?.detail ?? 'Failed to load',
          } as { jobs: RunningJobSummary[]; _error?: string })
        )
      ),
      blogging: this.bloggingApi.getJobs(false).pipe(catchError(() => of([]))),
      ai: this.aiSystemsApi.listJobs(false).pipe(catchError(() => of({ jobs: [] }))),
      prov: this.agentProvisioningApi.listJobs(false).pipe(catchError(() => of({ jobs: [] }))),
      social: this.socialMarketingApi.listJobs(false).pipe(catchError(() => of([]))),
      investment: this.investmentApi.listStrategyLabJobs(false).pipe(catchError(() => of({ jobs: [] }))),
      persona: this.personaApi.listJobs(false).pipe(catchError(() => of({ jobs: [] }))),
      sales: this.salesApi.listPipelineJobs(false).pipe(catchError(() => of([]))),
      planningV3: this.planningV3Api.getJobs().pipe(catchError(() => of({ jobs: [] }))),
      codingTeam: this.genericJobsApi.listJobs('coding_team').pipe(catchError(() => of({ jobs: [] }))),
      soc2: this.genericJobsApi.listJobs('soc2_compliance_team').pipe(catchError(() => of({ jobs: [] }))),
      pa: this.genericJobsApi.listJobs('personal_assistant_team').pipe(catchError(() => of({ jobs: [] }))),
      roadTrip: this.genericJobsApi.listJobs('road_trip_planning_team').pipe(catchError(() => of({ jobs: [] }))),
      nutrition: this.genericJobsApi.listJobs('nutrition_meal_planning_team').pipe(catchError(() => of({ jobs: [] }))),
    }).pipe(
      map(({ se, blogging, ai, prov, social, investment, persona, sales, planningV3, codingTeam, soc2, pa, roadTrip, nutrition }) => {
        this.seFetchError = (se as { _error?: string })._error ?? null;
        const seJobs = (se as { jobs: RunningJobSummary[] }).jobs;
        type RowWithSe = DashboardRow & { seSummary?: RunningJobSummary };
        const rows: RowWithSe[] = [];
        for (const s of seJobs) {
          rows.push({ unified: fromRunningJobSummary(s), seSummary: s });
        }
        for (const s of blogging) {
          rows.push({ unified: fromBlogJobListItem(s) });
        }
        for (const s of ai.jobs ?? []) {
          rows.push({ unified: fromAISystemJobSummary(s) });
        }
        for (const s of prov.jobs ?? []) {
          rows.push({ unified: fromProvisionJobSummary(s) });
        }
        for (const s of social) {
          rows.push({ unified: fromSocialMarketingJobListItem(s) });
        }
        for (const s of investment.jobs ?? []) {
          rows.push({ unified: fromInvestmentJobSummary(s) });
        }
        for (const s of persona.jobs ?? []) {
          rows.push({ unified: fromFounderJobSummary(s) });
        }
        for (const s of sales) {
          rows.push({ unified: fromSalesJobListItem(s) });
        }
        for (const s of planningV3.jobs ?? []) {
          rows.push({ unified: fromPlanningV3JobSummary(s) });
        }
        for (const s of codingTeam.jobs ?? []) {
          rows.push({ unified: fromGenericJobRecord('coding_team', s) });
        }
        for (const s of soc2.jobs ?? []) {
          rows.push({ unified: fromGenericJobRecord('soc2_compliance', s) });
        }
        for (const s of pa.jobs ?? []) {
          rows.push({ unified: fromGenericJobRecord('personal_assistant', s) });
        }
        for (const s of roadTrip.jobs ?? []) {
          rows.push({ unified: fromGenericJobRecord('road_trip_planning', s) });
        }
        for (const s of nutrition.jobs ?? []) {
          rows.push({ unified: fromGenericJobRecord('nutrition_meal_planning', s) });
        }
        rows.sort((a, b) => (b.unified.createdAt ?? '').localeCompare(a.unified.createdAt ?? ''));
        return rows;
      })
    );
  }

  /** Enrich rows that have seSummary with detail from SE APIs; return rows with seDetail set for SE. */
  private enrichSERows(rows: (DashboardRow & { seSummary?: RunningJobSummary })[]) {
    const toRow = (r: (typeof rows)[0], detail: SEDetail | null): DashboardRow => ({
      unified: r.unified,
      seDetail: detail ?? undefined,
    });
    const seIndices = rows
      .map((r, i) => (r.seSummary ? i : -1))
      .filter((i) => i >= 0);
    if (seIndices.length === 0) {
      return of(rows.map((r) => toRow(r, null)));
    }
    const detailRequests = seIndices.map((i) => this.fetchSEDetail(rows[i].seSummary!));
    return forkJoin(detailRequests).pipe(
      map((details) => {
        const detailBySeIndex = new Map(seIndices.map((j, idx) => [j, details[idx]]));
        return rows.map((r, i) => toRow(r, detailBySeIndex.get(i) ?? null));
      })
    );
  }

  private fetchSEDetail(summary: RunningJobSummary) {
    const jobType = summary.job_type;
    if (jobType === 'planning_v2') {
      return this.seApi.getPlanningV2Status(summary.job_id).pipe(
        map((status: PlanningV2StatusResponse) => this.toSEDetail({
          progress: status.progress,
          statusText: status.status_text,
          currentPhase: status.current_phase,
          waitingForAnswers: status.waiting_for_answers,
          teamProgress: { 'planning': { current_phase: status.current_phase, progress: status.progress } },
        })),
        catchError(() => of(null))
      );
    }
    if (jobType === 'product_analysis') {
      return this.seApi.getProductAnalysisStatus(summary.job_id).pipe(
        map((status: ProductAnalysisStatusResponse) => this.toSEDetail({
          progress: status.progress,
          statusText: status.status_text,
          currentPhase: status.current_phase,
          waitingForAnswers: status.waiting_for_answers,
          teamProgress: { 'product_analysis': { current_phase: status.current_phase, progress: status.progress } },
        })),
        catchError(() => of(null))
      );
    }
    if (jobType === 'backend_code_v2') {
      return this.seApi.getBackendCodeV2Status(summary.job_id).pipe(
        map((status) => this.toSEDetail({
          progress: status.progress,
          statusText: status.status_text,
          currentPhase: status.current_phase,
          teamProgress: { 'backend-code-v2': { current_phase: status.current_phase, progress: status.progress } },
        })),
        catchError(() => of(null))
      );
    }
    if (jobType === 'frontend_code_v2') {
      return this.seApi.getFrontendCodeV2Status(summary.job_id).pipe(
        map((status) => this.toSEDetail({
          progress: status.progress,
          statusText: status.status_text,
          currentPhase: status.current_phase,
          teamProgress: { 'frontend-code-v2': { current_phase: status.current_phase, progress: status.progress } },
        })),
        catchError(() => of(null))
      );
    }
    return this.seApi.getJobStatus(summary.job_id).pipe(
      map((status: JobStatusResponse) => this.toSEDetail({
        progress: status.progress,
        statusText: status.status_text,
        currentPhase: status.phase,
        waitingForAnswers: status.waiting_for_answers,
        teamProgress: status.team_progress,
      })),
      catchError(() => of(null))
    );
  }

  private toSEDetail(params: {
    progress?: number;
    statusText?: string;
    currentPhase?: string;
    waitingForAnswers?: boolean;
    teamProgress?: Record<string, TeamProgressEntry>;
  }): SEDetail {
    return {
      progress: params.progress,
      statusText: params.statusText,
      currentPhase: params.currentPhase,
      waitingForAnswers: params.waitingForAnswers,
      teamStatuses: this.buildTeamStatuses(params.teamProgress),
    };
  }

  private buildTeamStatuses(teamProgress?: Record<string, TeamProgressEntry>): TeamStatus[] {
    if (!teamProgress) return [];
    return Object.entries(teamProgress)
      .filter(([, entry]) => entry.current_phase)
      .map(([teamId, entry]) => {
        const displayInfo = TEAM_DISPLAY_INFO[teamId] ?? { label: teamId, icon: 'smart_toy' };
        const phase = entry.current_phase ?? '';
        const phaseLabel = PHASE_DISPLAY[phase] ?? phase.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
        return {
          teamId,
          label: displayInfo.label,
          icon: displayInfo.icon,
          phase,
          phaseLabel,
          isActive: phase !== 'completed' && phase !== '',
        };
      });
  }

  refresh(): void {
    this.loading = true;
    this.startPolling();
  }

  getJobTypeInfo(job: DashboardRow): JobTypeInfo {
    if (job.unified.source === 'software_engineering' && job.unified.jobType) {
      return JOB_TYPE_INFO[job.unified.jobType] ?? { label: job.unified.jobType, icon: 'work', route: '/software-engineering' };
    }
    const typeLabels: Record<string, JobTypeInfo> = {
      blogging: { label: 'Blog pipeline', icon: 'article', route: '/blogging' },
      ai_systems: { label: 'Build', icon: 'smart_toy', route: '/ai-systems' },
      agent_provisioning: { label: 'Provisioning', icon: 'settings', route: '/agent-provisioning' },
      social_marketing: { label: 'Campaign', icon: 'campaign', route: '/social-marketing' },
    };
    return typeLabels[job.unified.source] ?? { label: job.unified.source, icon: 'work', route: '/' };
  }

  getRepoName(repoPath?: string): string {
    if (!repoPath) return 'Unknown';
    const parts = repoPath.split('/');
    return parts[parts.length - 1] || repoPath;
  }

  getStatusClass(job: DashboardRow): string {
    if (job.seDetail?.waitingForAnswers) return 'status-waiting';
    switch (job.unified.status) {
      case 'running': return 'status-running';
      case 'completed': return 'status-completed';
      case 'failed': return 'status-failed';
      case 'cancelled': return 'status-cancelled';
      case 'interrupted': return 'status-interrupted';
      default: return 'status-pending';
    }
  }

  getStatusLabel(job: DashboardRow): string {
    if (job.seDetail?.waitingForAnswers) return 'Waiting';
    return (job.unified.status ?? '').charAt(0).toUpperCase() + (job.unified.status ?? '').slice(1);
  }

  getTimeAgo(createdAt?: string): string {
    if (!createdAt) return '';
    const created = new Date(createdAt);
    const now = new Date();
    const diffMs = now.getTime() - created.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    return `${diffDays}d ago`;
  }

  getActivityText(job: DashboardRow): string {
    if (job.seDetail?.waitingForAnswers) return 'Waiting for answers';
    if (job.seDetail?.statusText) return job.seDetail.statusText;
    if (job.seDetail?.currentPhase) {
      return job.seDetail.currentPhase.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
    }
    if (job.unified.phase) {
      return job.unified.phase.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
    }
    return '';
  }

  getProgress(job: DashboardRow): number | null {
    if (job.seDetail?.progress != null) return job.seDetail.progress;
    if (job.unified.progress != null) return job.unified.progress;
    return null;
  }

  getShowIndeterminate(job: DashboardRow): boolean {
    return job.unified.status === 'running' && !job.seDetail?.waitingForAnswers && this.getProgress(job) == null;
  }

  navigateToJob(job: DashboardRow): void {
    const u = job.unified;
    if (u.source === 'software_engineering' && u.jobType) {
      const info = this.getJobTypeInfo(job);
      const queryParams: Record<string, string | number> = { jobId: u.jobId };
      if (info.tabIndex !== undefined) {
        queryParams['tab'] = info.tabIndex;
      }
      this.router.navigate([info.route], { queryParams });
      return;
    }
    const info = SOURCE_DISPLAY[u.source];
    if (info) {
      this.router.navigate([info.route], { queryParams: { jobId: u.jobId } });
    }
  }

  stopJob(event: Event, job: DashboardRow): void {
    event.stopPropagation();
    if (!confirm(`Are you sure you want to stop the job for "${job.unified.label}"?`)) return;
    this.jobActions.stop(job.unified.source, job.unified.jobId).subscribe({
      next: () => this.refresh(),
      error: (err) => { this.error = err?.error?.detail ?? err?.message ?? 'Failed to stop job'; },
    });
  }

  resumeJob(event: Event, job: DashboardRow): void {
    event.stopPropagation();
    this.jobActions.resume(job.unified.source, job.unified.jobId).subscribe({
      next: () => this.refresh(),
      error: (err) => { this.error = err?.error?.detail ?? err?.message ?? 'Failed to resume job'; },
    });
  }

  restartJob(event: Event, job: DashboardRow): void {
    event.stopPropagation();
    if (!confirm(`Restart job for "${job.unified.label}" from scratch?`)) return;
    this.jobActions.restart(job.unified.source, job.unified.jobId).subscribe({
      next: () => this.refresh(),
      error: (err) => { this.error = err?.error?.detail ?? err?.message ?? 'Failed to restart job'; },
    });
  }

  deleteJob(event: Event, job: DashboardRow): void {
    event.stopPropagation();
    if (!confirm('Permanently delete this job? It will be removed from the list.')) return;
    this.jobActions.delete(job.unified.source, job.unified.jobId).subscribe({
      next: () => this.refresh(),
      error: (err) => { this.error = err?.error?.detail ?? err?.message ?? 'Failed to delete job'; },
    });
  }

  canStopJob(job: DashboardRow): boolean {
    return job.unified.status === 'running' || job.unified.status === 'pending';
  }

  canResumeJob(job: DashboardRow): boolean {
    return ['failed', 'interrupted', 'agent_crash', 'cancelled'].includes(job.unified.status);
  }

  canRestartJob(job: DashboardRow): boolean {
    return ['completed', 'failed', 'cancelled', 'interrupted', 'agent_crash'].includes(job.unified.status);
  }

  canDeleteJob(job: DashboardRow): boolean {
    return job.unified.status !== 'running' && job.unified.status !== 'pending';
  }

  trackByJobId(_index: number, job: DashboardRow): string {
    return `${job.unified.source}:${job.unified.jobId}`;
  }

  getPhaseColorClass(phase: string): string {
    switch (phase) {
      case 'setup':
      case 'planning':
        return 'phase-planning';
      case 'execution':
      case 'coding':
        return 'phase-execution';
      case 'review':
      case 'code_review':
      case 'qa_testing':
      case 'security_testing':
        return 'phase-review';
      case 'documentation':
        return 'phase-docs';
      case 'deliver':
      case 'completed':
        return 'phase-completed';
      case 'problem_solving':
        return 'phase-fixing';
      default:
        return 'phase-default';
    }
  }

  trackByTeamId(_index: number, team: TeamStatus): string {
    return team.teamId;
  }
}
