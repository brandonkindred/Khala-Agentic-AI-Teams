import { Component, DestroyRef, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatPaginatorModule, PageEvent } from '@angular/material/paginator';
import { RouterLink } from '@angular/router';
import { Subject, Subscription, timer, of } from 'rxjs';
import { catchError, switchMap, tap } from 'rxjs/operators';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
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
import type { TeamAssistantFieldSpec } from '../../models/team-assistant.model';
import { isCodingTeamTerminalStatus } from '../../models/job-status.model';

/** How often the Runs list is re-fetched while the page is open. */
const RUNS_POLL_MS = 15000;

/**
 * Precomputed view-model for one run row, so the Jobs accordion template binds plain properties
 * instead of calling helper methods per change-detection cycle. Rebuilt from `runs` in `applyRuns`
 * (every poll), so the relative `timeAgo` refreshes on the poll cadence.
 */
interface RunRowVm {
  run: CodingTeamJobListItem;
  issueNumber?: number;
  status: string;
  badgeClass: string;
  waiting: boolean;
  /** Live status/phase line for active runs; null when there's nothing to show (no empty tooltip). */
  detail: string | null;
  timeAgo: string;
}

/**
 * Precomputed view-model for one issue row, so the GitHub list template binds plain properties
 * instead of calling helper methods per change-detection cycle. Rebuilt whenever the visible page or
 * the "In progress" chip set changes.
 */
interface IssueRowVm {
  issue: GitHubIssueItem;
  number: number;
  title: string;
  labels: string[];
  inProgress: boolean;
  hasDeps: boolean;
  blocked: boolean;
  openDepsCount: number;
  depsTooltip: string;
}

@Component({
  selector: 'app-coding-team-page',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatIconModule,
    MatButtonModule,
    MatButtonToggleModule,
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
  private readonly destroyRef = inject(DestroyRef);

  latestJobId: string | null = null;

  /** Which single view is visible. The page opens on the assistant chat. */
  activeView: 'chat' | 'github' | 'jobs' = 'chat';

  // Embedded assistant chat configuration — named properties rather than template literals, so the
  // chat panel's wiring lives in one place.
  /** Assistant workflow endpoint for the embedded chat. */
  readonly teamApiUrl = '/api/coding-team/assistant';
  readonly chatTeamName = 'Coding Team';
  readonly chatTeamDescription = 'Software Engineering sub-team — task graph and implementation';
  readonly chatFields: TeamAssistantFieldSpec[] = [
    { key: 'repo_path', label: 'Repository path', placeholder: 'Path to project repository', required: true },
    { key: 'plan_input', label: 'Plan / tasks (optional)', placeholder: 'Feature descriptions or structured plan hints' },
  ];

  healthCheck = (): ReturnType<CodingTeamApiService['health']> => this.api.health();

  // GitHub integration state
  githubConfigured = false;
  githubOwner = '';
  githubRepo = '';
  isLoadingConfig = true;

  // Issue list
  issues: GitHubIssueItem[] = [];
  loadingIssues = false;
  issuesLoaded = false;
  issueError: string | null = null;
  /** View-models for the visible issue page; rebuilt in `recomputeIssueVms`. */
  pagedIssueVms: IssueRowVm[] = [];

  // Issue list pagination (client-side over the fully-fetched issue array)
  readonly PAGE_SIZE_OPTIONS = [10, 25, 50];
  /** Smallest configured page size; the paginator is shown only when the list exceeds it. */
  readonly paginatorThreshold = Math.min(...this.PAGE_SIZE_OPTIONS);
  pageSize = 10;
  pageIndex = 0;

  // Issue selection & confirmation
  private _selectedIssue: GitHubIssueItem | null = null;
  /** Open-dependency refs ("#3, #5") for the selected issue, precomputed so the confirm panel binds a
   * plain string instead of calling `openDepRefs` each change-detection cycle. */
  selectedIssueOpenDepsText = '';
  runningIssue = false;

  get selectedIssue(): GitHubIssueItem | null {
    return this._selectedIssue;
  }

  /** Setting the selected issue refreshes its precomputed open-dependency refs. */
  set selectedIssue(issue: GitHubIssueItem | null) {
    this._selectedIssue = issue;
    this.selectedIssueOpenDepsText = issue ? this.openDepRefs(issue) : '';
  }

  // Runs panel — the persistent, non-dismissable status panel.
  /** Every coding-team run for this repo (running + recent terminal), newest snapshot from /jobs. */
  runs: CodingTeamJobListItem[] = [];
  /** Non-terminal runs, for the "Running" section. Derived from `runs` in `applyRuns`. */
  runningRuns: CodingTeamJobListItem[] = [];
  /** Terminal runs, for the "Recent" section. Derived from `runs` in `applyRuns`. */
  recentRuns: CodingTeamJobListItem[] = [];
  /** View-models for the Running / Recent run rows; rebuilt from `runs` in `applyRuns`. */
  runningRunVms: RunRowVm[] = [];
  recentRunVms: RunRowVm[] = [];
  runsError: string | null = null;
  /** The run whose live detail is shown; null when nothing is selected. */
  selectedRunId: string | null = null;
  /** Issue number of the selected run — kept so the chip survives a list snapshot that lags a just-started run. */
  selectedRunNumber: number | null = null;
  /** Latest polled status of `selectedRunId`; null until the first poll lands. */
  private _jobStatus: CodingTeamJobStatus | null = null;
  /** Badge modifier for the selected run's status, precomputed from `jobStatus` so the detail panel
   * binds a plain field instead of calling `badgeClass` each change-detection cycle. */
  jobStatusBadgeClass = 'neutral';
  /** Whether the selected run has reached a terminal state, precomputed from `jobStatus` (the detail
   * panel binds this instead of calling `isJobTerminal()`). */
  jobStatusTerminal = false;

  get jobStatus(): CodingTeamJobStatus | null {
    return this._jobStatus;
  }

  /** Setting the polled status refreshes the precomputed badge class and terminal flag. */
  set jobStatus(status: CodingTeamJobStatus | null) {
    this._jobStatus = status;
    this.jobStatusBadgeClass = this.badgeClass(status?.status);
    this.jobStatusTerminal = isCodingTeamTerminalStatus(status?.status);
  }

  /**
   * Set when the selected run's status poll gives up, so the detail panel shows an error instead of
   * an indefinite "Starting…" spinner when the very first status never arrives. Cleared whenever a
   * fresh poll starts (new selection or resubmitted answers).
   */
  jobStatusError: string | null = null;

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
  /** Handle for the copy-confirmation reset, cleared on destroy so it never fires on a dead view. */
  private copyResetTimer: ReturnType<typeof setTimeout> | null = null;

  ngOnInit(): void {
    this.checkGitHubConfig();
  }

  ngOnDestroy(): void {
    this.stopPolling();
    this.runsSub?.unsubscribe();
    this.runsSub = null;
    // Drop the selected-run bookkeeping so nothing pairs with a dead view.
    this.selectedRunId = null;
    this.selectedRunNumber = null;
    if (this.copyResetTimer) {
      clearTimeout(this.copyResetTimer);
      this.copyResetTimer = null;
    }
    this.refreshTrigger$.complete();
  }

  /**
   * Load the configured GitHub integration (enabled flag, token, owner/repo) and gate the page on
   * it. On success, marks the page configured only when all of enabled/token/owner/repo are present
   * and — once the owner/repo is known — starts the Runs poll and kicks off the first issue load;
   * on error, leaves the page in the unconfigured state. Sets `isLoadingConfig` for the duration.
   */
  checkGitHubConfig(): void {
    this.isLoadingConfig = true;
    this.integrationsApi
      .getGitHubConfig()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (cfg) => {
        this.githubConfigured = cfg.enabled && cfg.token_configured && !!cfg.owner && !!cfg.repo;
        this.githubOwner = cfg.owner;
        this.githubRepo = cfg.repo;
        this.isLoadingConfig = false;
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
        this.isLoadingConfig = false;
      },
    });
  }

  /**
   * Refresh the open-issue list and the Runs snapshot together so they never drift. Resets the
   * selection/pagination, triggers a runs refresh (which re-syncs the "In progress" chips), then
   * fetches issues. Sets `issueError` on failure.
   *
   * Preconditions: none.
   * Postconditions: a no-op while a load is already in flight (`loadingIssues`), so a rapid
   * re-trigger never issues overlapping requests that could land out of order.
   */
  loadIssues(): void {
    if (this.loadingIssues) return;
    this.loadingIssues = true;
    this.issueError = null;
    this.selectedIssue = null;
    // Refreshing the issue list also refreshes the Runs list and the "In progress" chips, so the
    // two lists never drift.
    this.refreshTrigger$.next();
    this.integrationsApi
      .getGitHubIssues()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (issues) => {
        this.issues = issues;
        this.pageIndex = 0;
        this.issuesLoaded = true;
        this.loadingIssues = false;
        this.recomputeIssueVms();
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

  /**
   * Handle a paginator event: adopt the new page index/size and rebuild the visible issue view-models.
   *
   * Preconditions: `event` carries the paginator's new `pageIndex`/`pageSize`.
   * Postconditions: `pageIndex`/`pageSize` reflect `event` and `pagedIssueVms` matches the new slice.
   */
  onPageChange(event: PageEvent): void {
    this.pageIndex = event.pageIndex;
    this.pageSize = event.pageSize;
    this.recomputeIssueVms();
  }

  /** Select an issue, surfacing the run-confirmation affordance inline under its row. */
  selectIssue(issue: GitHubIssueItem): void {
    this.selectedIssue = issue;
  }

  /** Clear the issue selection, collapsing the inline confirmation panel. */
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

  /** Rebuild the Running/Recent run-row view-models from the current `runningRuns`/`recentRuns`. */
  private buildRunVms(): void {
    this.runningRunVms = this.runningRuns.map((r) => this.toRunVm(r));
    this.recentRunVms = this.recentRuns.map((r) => this.toRunVm(r));
  }

  /**
   * Build one run row's view-model so the template binds plain fields instead of calling helpers.
   *
   * Preconditions: none.
   * Postconditions: returns a `RunRowVm`; `detail` is null for a terminal run (no live status line).
   */
  private toRunVm(run: CodingTeamJobListItem): RunRowVm {
    return {
      run,
      issueNumber: run.github_context?.issue_number,
      status: run.status,
      badgeClass: this.badgeClass(run.status),
      waiting: this.isRunWaiting(run),
      detail: this.isRunActive(run) ? run.status_text || run.phase || null : null,
      timeAgo: this.timeAgo(run.updated_at),
    };
  }

  /** Rebuild the visible issue-row view-models (current page × current "In progress" chip set). */
  private recomputeIssueVms(): void {
    this.pagedIssueVms = this.pagedIssues.map((issue) => ({
      issue,
      number: issue.number,
      title: issue.title,
      labels: issue.labels,
      inProgress: this.isIssueInProgress(issue),
      hasDeps: this.hasDependencies(issue),
      blocked: issue.blocked,
      openDepsCount: issue.open_dependencies.length,
      depsTooltip: this.dependencyTooltip(issue),
    }));
  }

  /**
   * Start a coding-team run for the selected issue.
   *
   * Preconditions: none enforced — a no-op when `selectedIssue` is null or a run is already starting
   * (`runningIssue`), so a double-click can't submit the same issue twice.
   * Postconditions: on success the issue is marked in progress (`activeIssueNumbers`), the returned
   * run is selected (so its live detail shows immediately), the Runs list is refreshed, and the
   * selection is cleared; on error `issueError` is surfaced. `runningIssue` is toggled across the call.
   */
  confirmAndRun(): void {
    if (!this.selectedIssue || this.runningIssue) return;
    this.runningIssue = true;
    this.issueError = null;
    this.integrationsApi
      .runGitHubIssue({ issue_number: this.selectedIssue.number })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (resp: RunGitHubIssueResponse) => {
        this.runningIssue = false;
        this.selectedIssue = null;
        this.activeIssueNumbers.add(resp.issue_number);
        // Set the selected run's issue first so selectRun (which can't find the run in `runs`
        // until the next list tick) doesn't clear it.
        this.selectedRunNumber = resp.issue_number;
        this.selectRun(resp.job_id);
        this.recomputeIssueVms();
        this.refreshTrigger$.next();
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.issueError = err?.error?.detail || err?.message || 'Failed to start job.';
        this.runningIssue = false;
      },
    });
  }

  /**
   * Re-run the selected (typically terminal) run's issue, e.g. after a failure.
   *
   * Preconditions: none enforced — a no-op when the selected run has no resolvable issue number or a
   * run is already starting (`runningIssue`).
   * Postconditions: on success a fresh run for the same issue is started and selected, and the issue
   * is marked in progress; on error `issueError` is surfaced. `runningIssue` is toggled across the call.
   */
  retrySelectedRun(): void {
    const issueNumber = this.selectedRunNumber ?? this.jobStatus?.github_context?.issue_number;
    if (issueNumber == null || this.runningIssue) return;
    this.runningIssue = true;
    this.issueError = null;
    this.integrationsApi
      .runGitHubIssue({ issue_number: issueNumber })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (resp: RunGitHubIssueResponse) => {
        this.runningIssue = false;
        this.activeIssueNumbers.add(resp.issue_number);
        this.selectedRunNumber = resp.issue_number;
        this.selectRun(resp.job_id);
        this.recomputeIssueVms();
        this.refreshTrigger$.next();
      },
      error: (err: { error?: { detail?: string }; message?: string }) => {
        this.issueError = err?.error?.detail || err?.message || 'Failed to start job.';
        this.runningIssue = false;
      },
    });
  }

  /**
   * Subscribe to the Runs list poll.
   *
   * Preconditions: the GitHub integration is configured (`githubOwner`/`githubRepo` set) — the caller
   * only invokes this once configured, so jobs from other repositories are never matched here.
   * Postconditions: each `refreshTrigger$` emission re-fetches `/jobs` (terminal jobs included, so
   * "Recent" runs show) on a `timer(0, RUNS_POLL_MS)` cadence and applies them via `applyRuns`; a
   * failed fetch sets `runsError` and leaves the page usable. Idempotent — a second call while a
   * subscription is live is a no-op.
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
   * Postconditions: `runs` holds this repo's runs (running + terminal) and `runningRuns`/`recentRuns`
   * hold its non-terminal/terminal partitions; `activeIssueNumbers` holds the non-terminal subset's
   * issue numbers, plus the selected run's issue only while that run is absent from this snapshot and
   * not yet observed terminal (so a snapshot that lags a just-started run can't wipe its chip, while
   * a run the snapshot already reports terminal is trusted and dropped). On the first load only, a
   * run is auto-selected when none is selected.
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
    this.runningRuns = mine.filter((j) => !isCodingTeamTerminalStatus(j.status));
    this.recentRuns = mine.filter((j) => isCodingTeamTerminalStatus(j.status));

    const active = new Set(
      this.runningRuns
        .map((j) => j.github_context?.issue_number)
        .filter((n): n is number => n != null),
    );
    // Preserve the chip for a just-started run the snapshot does not list yet, but only while the run
    // is genuinely absent from the snapshot and not yet observed terminal. Once the snapshot lists the
    // run we trust the snapshot's own status — so a finished run is dropped and selecting a terminal
    // (Recent) run never re-adds an "In progress" chip. Keying off the snapshot, not the possibly
    // stale polled `jobStatus`, avoids a chip lingering for seconds after a run completes.
    const selectedInSnapshot =
      this.selectedRunId != null && mine.some((j) => j.job_id === this.selectedRunId);
    if (
      this.selectedRunId != null &&
      this.selectedRunNumber != null &&
      !selectedInSnapshot &&
      !isCodingTeamTerminalStatus(this.jobStatus?.status)
    ) {
      active.add(this.selectedRunNumber);
    }
    this.activeIssueNumbers = active;
    this.buildRunVms();
    this.recomputeIssueVms();

    if (this.initialRunsLoad) {
      this.initialRunsLoad = false;
      this.autoSelectRun(mine);
    }
  }

  /**
   * On the first list load, surface an in-flight run so a page reload restores the live panel.
   *
   * Preconditions: called once, from the first `applyRuns`; `runs` is this repo's filtered snapshot.
   * Postconditions: a no-op when a run is already selected or none is non-terminal; otherwise selects
   * a run paused on questions (so human-in-the-loop runs are reachable immediately) when present,
   * else the most recently updated non-terminal run.
   */
  private autoSelectRun(runs: CodingTeamJobListItem[]): void {
    if (this.selectedRunId) return;
    const active = runs.filter((r) => !isCodingTeamTerminalStatus(r.status));
    if (active.length === 0) return;
    const waiting = active.find((r) => r.waiting_for_answers);
    // ISO-8601 timestamps sort lexicographically in chronological order, so localeCompare picks the
    // most recently updated run without parsing dates.
    const pick =
      waiting ??
      active.reduce((best, j) =>
        (j.updated_at ?? '').localeCompare(best.updated_at ?? '') > 0 ? j : best,
      );
    this.selectRun(pick.job_id);
  }

  /**
   * Show a run's live detail and start polling its status.
   *
   * Preconditions: `jobId` is a coding-team job id; when it is a row in `runs` it carries a
   * `github_context.issue_number` (the list is pre-filtered to issue-bearing runs).
   * Postconditions: a no-op when `jobId` is already selected; otherwise `selectedRunId` is `jobId`,
   * `selectedRunNumber` is taken from the matching run when it is in `runs` (its issue number, or null
   * if the run somehow lacks one — the list is pre-filtered to issue-bearing runs, so this is a
   * defensive fallback), and left untouched when the run is not yet in `runs` (e.g. just started, so
   * the caller's pre-set number survives); `jobStatus`/`issueError`/`jobStatusError` are cleared, and
   * status polling for `jobId` is (re)started.
   */
  selectRun(jobId: string): void {
    if (this.selectedRunId === jobId) return;
    this.selectedRunId = jobId;
    const run = this.runs.find((r) => r.job_id === jobId);
    if (run) {
      this.selectedRunNumber = run.github_context?.issue_number ?? null;
    }
    this.jobStatus = null;
    this.issueError = null;
    this.jobStatusError = null;
    this.startPolling(jobId);
  }

  /**
   * Toggle a run row in the Jobs accordion: expand it (select + poll) when collapsed, or collapse it
   * (deselect + stop polling) when it is the open one.
   *
   * Preconditions: `run` is a row in `runs`.
   * Postconditions: when `run` was the selected row, `selectedRunId`/`selectedRunNumber`/`jobStatus`
   * are cleared and the status poll is stopped (the 15s list poll keeps the row's badge fresh) — so a
   * later snapshot can't re-add a stale "In progress" chip for the deselected run; otherwise `run`
   * becomes the selected, expanded row and its status poll starts.
   */
  toggleRun(run: CodingTeamJobListItem): void {
    if (this.selectedRunId === run.job_id) {
      this.stopPolling();
      this.selectedRunId = null;
      // Clear the issue number too: applyRuns keeps a chip alive for the selected run's issue, so a
      // lingering selectedRunNumber would re-flag a deselected (and possibly finished) issue.
      this.selectedRunNumber = null;
      this.jobStatus = null;
      this.jobStatusError = null;
      return;
    }
    this.selectRun(run.job_id);
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

  /**
   * Map a job status to a shared `.kh-badge--*` modifier.
   *
   * Preconditions: none (any string or undefined is accepted).
   * Postconditions: returns one of `running`/`completed`/`failed`/`cancelled`/`warning`/`neutral`;
   * unrecognized or missing statuses map to `neutral`.
   */
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

  /**
   * True when a run is still in flight (non-terminal). Scopes run-row affordances that only make
   * sense for active runs (the "needs answers" badge and the live status/phase detail line).
   *
   * Preconditions: none.
   * Postconditions: returns `false` for any terminal status, `true` otherwise.
   */
  isRunActive(run: CodingTeamJobListItem): boolean {
    return !isCodingTeamTerminalStatus(run.status);
  }

  /**
   * True when a run is actively paused on questions the user must answer.
   *
   * Preconditions: none.
   * Postconditions: returns `true` only when the run is non-terminal and flagged
   * `waiting_for_answers` — so a terminal run that still carries a stale flag is never shown as
   * needing answers.
   */
  isRunWaiting(run: CodingTeamJobListItem): boolean {
    return !!run.waiting_for_answers && this.isRunActive(run);
  }

  /**
   * Relative "x ago" label for a run's last update.
   *
   * Preconditions: none.
   * Postconditions: returns `''` for a missing or unparseable timestamp (so a malformed value can
   * never render as "NaNd ago"), else `just now` / `Nm ago` / `Nh ago` / `Nd ago` for the elapsed
   * time since `isoString`.
   */
  timeAgo(isoString?: string): string {
    if (!isoString) return '';
    const date = new Date(isoString);
    if (isNaN(date.getTime())) return '';
    const diff = Date.now() - date.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  }

  /**
   * Copy the selected run's full job id to the clipboard, flashing a confirmation icon.
   *
   * Preconditions: none (a no-op when no run is selected).
   * Postconditions: when a run is selected and the Clipboard API is available, its id is written to
   * the clipboard and `jobIdCopied` is true for ~1.5s (the reset timer is tracked so it is cancelled
   * on destroy and never fires twice); a rejected clipboard write is swallowed so it cannot surface
   * as an unhandled rejection. When the Clipboard API is unavailable the confirmation still flashes.
   */
  copyJobId(): void {
    if (!this.selectedRunId) return;
    navigator.clipboard?.writeText(this.selectedRunId).catch(() => {
      // Clipboard write can reject (permission denied, insecure context); ignore — the user can
      // still read the id from the panel, and we must not emit an unhandled rejection.
    });
    this.jobIdCopied = true;
    if (this.copyResetTimer) clearTimeout(this.copyResetTimer);
    this.copyResetTimer = setTimeout(() => {
      this.jobIdCopied = false;
      this.copyResetTimer = null;
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
    // member is always the coding-team status shape — but verify the discriminating fields are
    // present rather than asserting blindly, so a future change to the child's emission can't
    // silently fold a foreign shape into `jobStatus`.
    if ('job_id' in status && 'status' in status) {
      this.jobStatus = status as CodingTeamJobStatus;
    }
    this.issueError = null;
    this.jobStatusError = null;
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
   * when no run is selected or a resume is already in flight (`resumingJob`), so a double-click can't
   * fire overlapping resume requests. On success, restarts polling from scratch; on error, surfaces
   * `issueError`.
   */
  resumeJob(): void {
    if (!this.selectedRunId || this.resumingJob) return;
    const jobId = this.selectedRunId;
    this.resumingJob = true;
    this.issueError = null;
    this.api
      .resumeJob(jobId)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
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
        // The status poll gave up: surface it on the page banner and in the run detail, so a
        // never-arriving first status shows an error instead of a perpetual "Starting…" spinner.
        this.issueError = 'Lost connection to the coding team — status polling failed.';
        this.jobStatusError = this.issueError;
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

  /** Record the job id emitted when the assistant launches a coding workflow (drives the banner). */
  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    this.latestJobId = event.job_id;
  }
}
