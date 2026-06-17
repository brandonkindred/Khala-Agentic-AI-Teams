import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatPaginatorModule, PageEvent } from '@angular/material/paginator';
import { RouterLink } from '@angular/router';
import { Subject, Subscription, timer, of } from 'rxjs';
import { catchError, switchMap, tap } from 'rxjs/operators';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { pollJobStatus } from '../../services/job-status-poller';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { CodingTeamMonitorComponent } from '../coding-team-monitor/coding-team-monitor.component';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import {
  PendingQuestionsComponent,
  type AnswersSubmittedStatus,
} from '../pending-questions/pending-questions.component';
import type { GitHubIssueItem, RunGitHubIssueResponse } from '../../models/integrations.model';
import type { CodingTeamJobListItem, CodingTeamJobStatus } from '../../models/coding-team.model';
import { isCodingTeamTerminalStatus } from '../../models/job-status.model';

/** How often the Runs list is re-fetched while the page is open. */
const RUNS_POLL_MS = 15000;

@Component({
  selector: 'app-coding-team-page',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatIconModule,
    MatButtonModule,
    MatProgressSpinnerModule,
    MatChipsModule,
    MatTooltipModule,
    MatExpansionModule,
    MatPaginatorModule,
    RouterLink,
    HealthIndicatorComponent,
    CodingTeamMonitorComponent,
    TeamAssistantChatComponent,
    PendingQuestionsComponent,
  ],
  templateUrl: './coding-team-page.component.html',
  styleUrl: './coding-team-page.component.scss',
})
export class CodingTeamPageComponent implements OnInit, OnDestroy {
  private readonly api = inject(CodingTeamApiService);
  private readonly integrationsApi = inject(IntegrationsApiService);

  latestJobId: string | null = null;

  healthCheck = (): ReturnType<CodingTeamApiService['health']> => this.api.health();

  // GitHub integration state
  githubConfigured = false;
  githubOwner = '';
  githubRepo = '';
  loadingConfig = true;

  // Issue list
  issues: GitHubIssueItem[] = [];
  loadingIssues = false;
  issuesLoaded = false;
  issueError: string | null = null;

  // Issue list pagination (client-side over the fully-fetched issue array)
  readonly PAGE_SIZE_OPTIONS = [10, 25, 50];
  pageSize = 10;
  pageIndex = 0;

  // Issue selection & confirmation
  selectedIssue: GitHubIssueItem | null = null;
  runningIssue = false;

  // Runs panel — the persistent, non-dismissable status panel.
  /** Every coding-team run for this repo (running + recent terminal), newest snapshot from /jobs. */
  runs: CodingTeamJobListItem[] = [];
  runsError: string | null = null;
  /** The run whose live detail is shown; null when nothing is selected. */
  selectedRunId: string | null = null;
  /** Issue number of the selected run — kept so the chip survives a list snapshot that lags a just-started run. */
  selectedRunNumber: number | null = null;
  /** Latest polled status of `selectedRunId`; null until the first poll lands. */
  jobStatus: CodingTeamJobStatus | null = null;

  /** True while a manual resume POST is in flight (drives a spinner on the Resume button). */
  resumingJob = false;
  /** True for a short window after the job id is copied, to flip the copy icon to a check. */
  jobIdCopied = false;

  /** Issue numbers with a non-terminal coding-team run, for "In progress" chips. */
  activeIssueNumbers = new Set<number>();

  private pollSub: Subscription | null = null;
  private runsSub: Subscription | null = null;
  private readonly refreshTrigger$ = new Subject<void>();
  /** Auto-select a run only on the first list load, so later polls never steal the user's selection. */
  private initialRunsLoad = true;

  ngOnInit(): void {
    this.checkGitHubConfig();
  }

  ngOnDestroy(): void {
    this.stopPolling();
    this.runsSub?.unsubscribe();
    this.runsSub = null;
    this.refreshTrigger$.complete();
  }

  /**
   * Load the configured GitHub integration (enabled flag, token, owner/repo) and gate the page on
   * it. On success, marks the page configured only when all of enabled/token/owner/repo are present
   * and — once the owner/repo is known — starts the Runs poll and kicks off the first issue load;
   * on error, leaves the page in the unconfigured state. Sets `loadingConfig` for the duration.
   */
  checkGitHubConfig(): void {
    this.loadingConfig = true;
    this.integrationsApi.getGitHubConfig().subscribe({
      next: (cfg) => {
        this.githubConfigured = cfg.enabled && cfg.token_configured && !!cfg.owner && !!cfg.repo;
        this.githubOwner = cfg.owner;
        this.githubRepo = cfg.repo;
        this.loadingConfig = false;
        if (this.githubConfigured) {
          // The Runs poll only starts once the configured owner/repo is known, so runs from other
          // repositories are never matched against this page's issues; loadIssues then triggers the
          // first list fetch alongside the issues.
          this.startRunsPolling();
          this.loadIssues();
        }
      },
      error: () => {
        this.githubConfigured = false;
        this.loadingConfig = false;
      },
    });
  }

  /**
   * Refresh the open-issue list and the Runs snapshot together so they never drift. Resets the
   * selection/pagination, triggers a runs refresh (which re-syncs the "In progress" chips), then
   * fetches issues. Sets `issueError` on failure.
   */
  loadIssues(): void {
    this.loadingIssues = true;
    this.issueError = null;
    this.selectedIssue = null;
    // Refreshing the issue list also refreshes the Runs list and the "In progress" chips, so the
    // two lists never drift.
    this.refreshTrigger$.next();
    this.integrationsApi.getGitHubIssues().subscribe({
      next: (issues) => {
        this.issues = issues;
        this.pageIndex = 0;
        this.issuesLoaded = true;
        this.loadingIssues = false;
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.issueError = err?.error?.detail || err?.message || 'Failed to load issues.';
        this.loadingIssues = false;
      },
    });
  }

  /** The slice of issues visible on the current page. */
  get pagedIssues(): GitHubIssueItem[] {
    const start = this.pageIndex * this.pageSize;
    return this.issues.slice(start, start + this.pageSize);
  }

  onPageChange(event: PageEvent): void {
    this.pageIndex = event.pageIndex;
    this.pageSize = event.pageSize;
  }

  /** Select an issue, surfacing the run-confirmation affordance inline under its row. */
  selectIssue(issue: GitHubIssueItem): void {
    this.selectedIssue = issue;
  }

  cancelSelection(): void {
    this.selectedIssue = null;
  }

  /** True when the issue is blocked by, or depends on, one or more other issues. */
  hasDependencies(issue: GitHubIssueItem): boolean {
    return issue.dependencies.length > 0;
  }

  /** All dependencies rendered as "#3, #5". */
  allDepRefs(issue: GitHubIssueItem): string {
    return issue.dependencies.map((d) => `#${d.number}`).join(', ');
  }

  /** The still-open dependencies rendered as "#3, #5" for warnings and tooltips. */
  openDepRefs(issue: GitHubIssueItem): string {
    return issue.dependencies
      .filter((d) => d.state === 'open')
      .map((d) => `#${d.number}`)
      .join(', ');
  }

  /** Hover/aria text describing the issue's dependencies and whether they block it. */
  dependencyTooltip(issue: GitHubIssueItem): string {
    return issue.blocked
      ? `Blocked by ${this.openDepRefs(issue)} — must be closed first`
      : `Depends on ${this.allDepRefs(issue)} (all complete)`;
  }

  /**
   * Start a coding-team run for the selected issue. No-op when nothing is selected. On success,
   * marks its issue in progress, selects the new run (so its live detail shows immediately),
   * refreshes the Runs list, and clears the selection; on error, surfaces `issueError`. Toggles
   * `runningIssue`.
   */
  confirmAndRun(): void {
    if (!this.selectedIssue) return;
    this.runningIssue = true;
    this.issueError = null;
    this.integrationsApi.runGitHubIssue({ issue_number: this.selectedIssue.number }).subscribe({
      next: (resp: RunGitHubIssueResponse) => {
        this.runningIssue = false;
        this.selectedIssue = null;
        this.activeIssueNumbers.add(resp.issue_number);
        // Set the selected run's issue first so selectRun (which can't find the run in `runs`
        // until the next list tick) doesn't clear it.
        this.selectedRunNumber = resp.issue_number;
        this.selectRun(resp.job_id);
        this.refreshTrigger$.next();
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.issueError = err?.error?.detail || err?.message || 'Failed to start job.';
        this.runningIssue = false;
      },
    });
  }

  /**
   * Re-run a terminal selected run's issue (e.g. after a failure). No-op when the selected run has
   * no issue number. Starts a fresh run for the same issue and selects it.
   */
  retrySelectedRun(): void {
    const issueNumber = this.selectedRunNumber ?? this.jobStatus?.github_context?.issue_number;
    if (issueNumber == null || this.runningIssue) return;
    this.runningIssue = true;
    this.issueError = null;
    this.integrationsApi.runGitHubIssue({ issue_number: issueNumber }).subscribe({
      next: (resp: RunGitHubIssueResponse) => {
        this.runningIssue = false;
        this.activeIssueNumbers.add(resp.issue_number);
        this.selectedRunNumber = resp.issue_number;
        this.selectRun(resp.job_id);
        this.refreshTrigger$.next();
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.issueError = err?.error?.detail || err?.message || 'Failed to start job.';
        this.runningIssue = false;
      },
    });
  }

  /**
   * Subscribe to the Runs list poll. Each tick (and every manual refresh via `refreshTrigger$`)
   * re-fetches `/jobs` — including terminal jobs, so "Recent" runs are shown — and applies them.
   * Idempotent: a second call is a no-op while a subscription is live.
   */
  private startRunsPolling(): void {
    if (this.runsSub) return;
    this.runsSub = this.refreshTrigger$
      .pipe(
        switchMap(() => timer(0, RUNS_POLL_MS)),
        switchMap(() =>
          // listJobs(false): include terminal jobs so finished runs appear under "Recent".
          this.api.listJobs(false).pipe(
            // Clear the error only on a successful fetch — the catchError fallback below must not
            // wipe the error it just set.
            tap(() => {
              this.runsError = null;
            }),
            catchError((err: { error?: { detail?: string }; message?: string }) => {
              this.runsError = err?.error?.detail ?? err?.message ?? 'Failed to load runs.';
              return of([] as CodingTeamJobListItem[]);
            }),
          ),
        ),
      )
      .subscribe((jobs) => this.applyRuns(jobs));
  }

  /**
   * Fold a fresh `/jobs` snapshot into the Runs panel.
   *
   * Preconditions: `githubOwner`/`githubRepo` are set (the poll only starts once configured).
   * Postconditions: `runs` holds this repo's runs (running + terminal); `activeIssueNumbers` holds
   * the non-terminal subset's issue numbers, plus the selected run's issue while it is still
   * non-terminal (so a snapshot that lags a just-started run can't wipe its chip). On the first
   * load only, a run is auto-selected when none is selected.
   */
  private applyRuns(jobs: CodingTeamJobListItem[]): void {
    const owner = this.githubOwner.toLowerCase();
    const repo = this.githubRepo.toLowerCase();
    const mine = jobs.filter(
      (j) =>
        j.github_context?.issue_number != null &&
        j.github_context.owner.toLowerCase() === owner &&
        j.github_context.repo.toLowerCase() === repo,
    );
    this.runs = mine;

    const active = new Set(
      mine
        .filter((j) => !isCodingTeamTerminalStatus(j.status))
        .map((j) => j.github_context!.issue_number!),
    );
    // Keep the chip for the selected run while it is still running, even if this snapshot predates
    // it — but never once it has finished (the poller has already observed terminal by then).
    if (this.selectedRunNumber != null && !isCodingTeamTerminalStatus(this.jobStatus?.status)) {
      active.add(this.selectedRunNumber);
    }
    this.activeIssueNumbers = active;

    if (this.initialRunsLoad) {
      this.initialRunsLoad = false;
      this.autoSelectRun(mine);
    }
  }

  /**
   * On the first list load, surface an in-flight run so a page reload restores the live panel.
   * Prefers a run paused on questions (so human-in-the-loop runs are reachable immediately), else
   * the most recently updated non-terminal run. No-op when a run is already selected or none is
   * active.
   */
  private autoSelectRun(runs: CodingTeamJobListItem[]): void {
    if (this.selectedRunId) return;
    const active = runs.filter((r) => !isCodingTeamTerminalStatus(r.status));
    if (active.length === 0) return;
    const waiting = active.find((r) => r.waiting_for_answers);
    const pick =
      waiting ??
      active.reduce((best, j) =>
        (j.updated_at ?? '').localeCompare(best.updated_at ?? '') > 0 ? j : best,
      );
    this.selectRun(pick.job_id);
  }

  /** Show a run's live detail and start polling its status. No-op if it is already selected. */
  selectRun(jobId: string): void {
    if (this.selectedRunId === jobId) return;
    this.selectedRunId = jobId;
    const run = this.runs.find((r) => r.job_id === jobId);
    if (run?.github_context?.issue_number != null) {
      this.selectedRunNumber = run.github_context.issue_number;
    }
    this.jobStatus = null;
    this.issueError = null;
    this.startPolling(jobId);
  }

  /** The currently selected run's list row, or null. */
  get selectedRun(): CodingTeamJobListItem | null {
    return this.runs.find((r) => r.job_id === this.selectedRunId) ?? null;
  }

  /** GitHub issue URL for the selected run's header link, if known. */
  get selectedIssueUrl(): string {
    return (
      this.selectedRun?.github_context?.issue_url ??
      this.jobStatus?.github_context?.issue_url ??
      ''
    );
  }

  /** Non-terminal runs, for the "Running" section. */
  get runningRuns(): CodingTeamJobListItem[] {
    return this.runs.filter((r) => !isCodingTeamTerminalStatus(r.status));
  }

  /** Terminal runs, for the "Recent" section. */
  get recentRuns(): CodingTeamJobListItem[] {
    return this.runs.filter((r) => isCodingTeamTerminalStatus(r.status));
  }

  /** Map a job status to a shared `.kh-badge--*` modifier. */
  badgeClass(status: string | undefined): string {
    switch (status) {
      case 'running':
      case 'pending':
        return 'running';
      case 'completed':
        return 'completed';
      case 'failed':
        return 'failed';
      case 'cancelled':
        return 'cancelled';
      case 'completed_with_failures':
      case 'waiting_for_user':
        return 'warning';
      default:
        return 'neutral';
    }
  }

  /** Relative "x ago" label for a run's last update; empty for a missing timestamp. */
  timeAgo(isoString?: string): string {
    if (!isoString) return '';
    const diff = Date.now() - new Date(isoString).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  }

  /** Copy the selected run's full job id to the clipboard, flashing a confirmation icon. */
  copyJobId(): void {
    if (!this.selectedRunId) return;
    void navigator.clipboard?.writeText(this.selectedRunId);
    this.jobIdCopied = true;
    setTimeout(() => {
      this.jobIdCopied = false;
    }, 1500);
  }

  /** True when the job is paused on questions the user must answer. */
  hasPendingQuestions(): boolean {
    return !!this.jobStatus?.waiting_for_answers && (this.jobStatus?.pending_questions?.length ?? 0) > 0;
  }

  /**
   * Fold the post-submit status into the panel and restart polling from scratch.
   *
   * Restarting (rather than letting the old poller run on) discards any status fetch that was
   * already in flight before the answers were stored — a stale response would otherwise re-render
   * the just-answered questions — and also revives polling after a connection loss (answering proves
   * the connection is back), so the stale "lost connection" error is cleared too.
   */
  onAnswersSubmitted(status: AnswersSubmittedStatus): void {
    // This page always configures the panel with submitEndpoint="coding-team", so the emitted union
    // member is always the coding-team status shape.
    this.jobStatus = status as CodingTeamJobStatus;
    this.issueError = null;
    if (this.selectedRunId) {
      this.startPolling(this.selectedRunId);
    }
  }

  /** True when a non-terminal coding-team run is already working this issue. */
  isIssueInProgress(issue: GitHubIssueItem): boolean {
    return this.activeIssueNumbers.has(issue.number);
  }

  /**
   * Restart the selected run's orchestrator after answers were stored but auto-resume failed. No-op
   * when no run is selected. On success, restarts polling from scratch; on error, surfaces
   * `issueError`.
   */
  resumeJob(): void {
    if (!this.selectedRunId) return;
    const jobId = this.selectedRunId;
    this.resumingJob = true;
    this.issueError = null;
    this.api.resumeJob(jobId).subscribe({
      next: () => {
        this.resumingJob = false;
        this.startPolling(jobId);
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.resumingJob = false;
        this.issueError = err?.error?.detail ?? err?.message ?? 'Failed to resume job.';
      },
    });
  }

  private startPolling(jobId: string): void {
    this.stopPolling();
    this.pollSub = pollJobStatus(
      this.api,
      jobId,
      (status) => {
        // Discard a stale poll for a run the user has since switched away from.
        if (this.selectedRunId !== jobId) return;
        this.jobStatus = status;
        if (this.selectedRunNumber == null && status.github_context?.issue_number != null) {
          this.selectedRunNumber = status.github_context.issue_number;
        }
        // The selected run finished: refresh the list so it moves Running → Recent and drops its
        // "In progress" chip. The selection stays so its finished detail remains on screen.
        if (isCodingTeamTerminalStatus(status.status)) {
          this.refreshTrigger$.next();
        }
      },
      () => {
        this.issueError = 'Lost connection to the coding team — status polling failed.';
      },
    );
  }

  private stopPolling(): void {
    if (this.pollSub) {
      this.pollSub.unsubscribe();
      this.pollSub = null;
    }
  }

  /** True once the selected run has reached a terminal state (finished — pollable no further). */
  isJobTerminal(): boolean {
    return isCodingTeamTerminalStatus(this.jobStatus?.status);
  }

  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    this.latestJobId = event.job_id;
  }
}
