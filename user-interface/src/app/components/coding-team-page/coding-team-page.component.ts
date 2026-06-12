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
import { Subscription } from 'rxjs';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { pollJobStatus } from '../../services/job-status-poller';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import {
  PendingQuestionsComponent,
  type AnswersSubmittedStatus,
} from '../pending-questions/pending-questions.component';
import type { GitHubIssueItem, RunGitHubIssueResponse } from '../../models/integrations.model';
import type { CodingTeamJobStatus } from '../../models/coding-team.model';
import { isCodingTeamTerminalStatus } from '../../models/job-status.model';

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

  // Active job tracking
  activeJob: RunGitHubIssueResponse | null = null;
  jobStatus: CodingTeamJobStatus | null = null;
  private pollSub: Subscription | null = null;
  private restoreSub: Subscription | null = null;

  /** Issue numbers with a non-terminal coding-team job, for "In progress" chips. */
  activeIssueNumbers = new Set<number>();

  /** Jobs the user dismissed this session — never re-adopted into the panel automatically. */
  private dismissedJobIds = new Set<string>();

  ngOnInit(): void {
    this.checkGitHubConfig();
  }

  ngOnDestroy(): void {
    this.stopPolling();
    this.restoreSub?.unsubscribe();
    this.restoreSub = null;
  }

  /**
   * Load the configured GitHub integration (enabled flag, token, owner/repo) and gate the page on
   * it. On success, marks the page configured only when all of enabled/token/owner/repo are present
   * and — once the owner/repo is known — kicks off the first issue load; on error, leaves the page
   * in the unconfigured state. Sets `loadingConfig` for the duration.
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
          // loadIssues also refreshes the active-jobs snapshot; restore happens only
          // once the configured owner/repo is known, so jobs from other repositories
          // are never matched against this page's issues.
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
   * Refresh the open-issue list and the active-jobs snapshot together so they never drift. Resets
   * the selection/pagination, re-syncs the "In progress" chips (adopting an in-flight job if none
   * is shown) via `restoreActiveJob`, then fetches issues. Sets `issueError` on failure.
   */
  loadIssues(): void {
    this.loadingIssues = true;
    this.issueError = null;
    this.selectedIssue = null;
    // Refreshing the issue list also refreshes the "In progress" chips (and adopts an
    // in-flight job if none is shown), so the list and the jobs snapshot never drift.
    this.restoreActiveJob();
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

  /** Select an issue, surfacing the run-confirmation affordance for it. */
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
   * adopts the returned job as the active panel, marks its issue in progress, clears the selection,
   * and begins polling its status; on error, surfaces `issueError`. Toggles `runningIssue`.
   */
  confirmAndRun(): void {
    if (!this.selectedIssue) return;
    this.runningIssue = true;
    this.issueError = null;
    this.integrationsApi.runGitHubIssue({ issue_number: this.selectedIssue.number }).subscribe({
      next: (resp: RunGitHubIssueResponse) => {
        this.activeJob = resp;
        this.activeIssueNumbers.add(resp.issue_number);
        this.selectedIssue = null;
        this.runningIssue = false;
        this.startPolling(resp.job_id);
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.issueError = err?.error?.detail || err?.message || 'Failed to start job.';
        this.runningIssue = false;
      },
    });
  }

  /**
   * Re-attach the page to a coding-team run already in flight (e.g. after a
   * page reload), so the "working on issue #N" indicator and any pending
   * questions survive navigation. Also the single source for the issue list's
   * "In progress" chips — called on every issue refresh and on dismiss so the
   * chips track the server's view instead of local bookkeeping.
   *
   * Preconditions: the GitHub integration is configured (`githubOwner`/`githubRepo`
   * are set) — only jobs for this repository (owner/repo compared
   * case-insensitively, as GitHub does) are considered.
   * Postconditions: `activeIssueNumbers` holds every non-terminal job for this
   * repo plus the currently displayed job (a snapshot taken before a just-started
   * run must not wipe its chip); when no job is displayed, the most recently
   * updated non-dismissed one is adopted as `activeJob` and polling started
   * (the poller's first fetch is immediate). List fetch failures leave the page
   * usable (silent no-op).
   */
  private restoreActiveJob(): void {
    const owner = this.githubOwner.toLowerCase();
    const repo = this.githubRepo.toLowerCase();
    this.restoreSub?.unsubscribe();
    this.restoreSub = this.api.listJobs(true).subscribe({
      next: (jobs) => {
        // listJobs(true) requests ?active=true so terminal jobs' full records never cross the
        // wire; the terminal check below is a defensive belt — the backend's non-terminal status
        // set and this client's terminal list are maintained independently, and adopting a
        // finished job would pin a dead panel on screen if they ever drift.
        const candidates = jobs.filter(
          (j) =>
            !isCodingTeamTerminalStatus(j.status) &&
            j.github_context?.issue_number != null &&
            j.github_context.owner.toLowerCase() === owner &&
            j.github_context.repo.toLowerCase() === repo,
        );
        const issueNumbers = new Set(candidates.map((j) => j.github_context!.issue_number!));
        // Merge the displayed job's issue so a stale snapshot can't wipe a just-started run's
        // chip — but ONLY while that job is still non-terminal. Once it finishes, the poller has
        // already dropped its chip, and /jobs?active=true no longer lists it, so re-adding here
        // would wrongly re-label a completed issue "In progress" until the panel is dismissed.
        if (this.activeJob && !this.isJobTerminal()) {
          issueNumbers.add(this.activeJob.issue_number);
        }
        this.activeIssueNumbers = issueNumbers;

        const adoptable = candidates.filter((j) => !this.dismissedJobIds.has(j.job_id));
        if (this.activeJob || adoptable.length === 0) return;

        const mostRecent = adoptable.reduce((best, j) =>
          (j.updated_at ?? '').localeCompare(best.updated_at ?? '') > 0 ? j : best,
        );
        const ctx = mostRecent.github_context!;
        this.activeJob = {
          job_id: mostRecent.job_id,
          issue_number: ctx.issue_number!,
          issue_url: ctx.issue_url ?? '',
          status: mostRecent.status,
          message: '',
        };
        this.startPolling(mostRecent.job_id);
      },
      error: () => undefined,
    });
  }

  /** True when the job is paused on questions the user must answer. */
  hasPendingQuestions(): boolean {
    return !!this.jobStatus?.waiting_for_answers && (this.jobStatus?.pending_questions?.length ?? 0) > 0;
  }

  /**
   * Fold the post-submit status into the panel and restart polling from scratch.
   *
   * Restarting (rather than letting the old poller run on) discards any status
   * fetch that was already in flight before the answers were stored — a stale
   * response would otherwise re-render the just-answered questions — and also
   * revives polling after a connection loss (answering proves the connection
   * is back), so the stale "lost connection" error is cleared too.
   */
  onAnswersSubmitted(status: AnswersSubmittedStatus): void {
    // This page always configures the panel with submitEndpoint="coding-team",
    // so the emitted union member is always the coding-team status shape.
    this.jobStatus = status as CodingTeamJobStatus;
    this.issueError = null;
    if (this.activeJob) {
      this.startPolling(this.activeJob.job_id);
    }
  }

  /** True when a non-terminal coding-team job is already working this issue. */
  isIssueInProgress(issue: GitHubIssueItem): boolean {
    return this.activeIssueNumbers.has(issue.number);
  }

  private startPolling(jobId: string): void {
    this.stopPolling();
    this.pollSub = pollJobStatus(
      this.api,
      jobId,
      (status) => {
        this.jobStatus = status;
        // The watched job finished: its issue is no longer being worked on.
        if (this.activeJob && isCodingTeamTerminalStatus(status.status)) {
          this.activeIssueNumbers.delete(this.activeJob.issue_number);
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

  /** True once the active job has reached a terminal state (finished — pollable no further). */
  isJobTerminal(): boolean {
    return isCodingTeamTerminalStatus(this.jobStatus?.status);
  }

  /**
   * Dismiss the displayed job panel. Records the job id so it is never auto-re-adopted; drops its
   * issue chip only when the job is terminal (a still-running job the user merely hides keeps its
   * chip). Stops polling, clears the panel, then re-syncs chips and surfaces any other in-flight
   * job for this repo via `restoreActiveJob`.
   */
  dismissJob(): void {
    if (this.activeJob) {
      // Never auto-re-adopt a job the user explicitly dismissed.
      this.dismissedJobIds.add(this.activeJob.job_id);
      // A finished job's issue is no longer in progress; a still-running job the
      // user merely hides keeps its chip so the issue list stays truthful (the
      // refresh below confirms either way against the server).
      if (this.isJobTerminal()) {
        this.activeIssueNumbers.delete(this.activeJob.issue_number);
      }
    }
    this.stopPolling();
    this.activeJob = null;
    this.jobStatus = null;
    // Re-sync chips with the server and surface any other in-flight job for this
    // repo (e.g. one started in another tab) now that the panel is free.
    this.restoreActiveJob();
  }

  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    this.latestJobId = event.job_id;
  }
}
