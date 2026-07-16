import { ChangeDetectorRef, Component, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatPaginatorModule, PageEvent } from '@angular/material/paginator';
import { RouterLink } from '@angular/router';
import { Subject, Subscription } from 'rxjs';
import { takeUntil } from 'rxjs/operators';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { pollJobStatus } from '../../services/job-status-poller';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { PendingIssueProposalsComponent } from './pending-issue-proposals/pending-issue-proposals.component';
import type {
  CodeReviewRunItem,
  GitHubPullRequestItem,
  GitHubRepoItem,
  RunPrReviewResponse,
} from '../../models/integrations.model';
import type { CodeReviewSummary, CodingTeamJobStatus } from '../../models/coding-team.model';
import { isCodingTeamTerminalStatus } from '../../models/job-status.model';
import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import { extractErrorDetail } from '../../shared/extract-error-detail';
import { LatestOnly } from '../../shared/latest-only';

/**
 * One code-review run on a pull request. Held in memory and kept live by a
 * per-job poller; the authoritative copy is persisted backend-side (the
 * `code_review_runs` table) and re-hydrated on load so history survives reloads.
 */
export interface PrReviewRecord {
  jobId: string;
  prNumber: number;
  /** Repository the review ran against — PR numbers collide across repositories. */
  owner: string;
  repo: string;
  /** Milliseconds since epoch when the review started (for the table timestamp). */
  startedAt: number;
  /** Milliseconds since epoch when the review reached a terminal state, if known.
   * Drives the row's duration; absent while the review is still running. */
  completedAt?: number;
  status: string;
  statusText?: string;
  reviewSummary?: CodeReviewSummary;
  prUrl?: string;
  error?: string;
}

/**
 * Code Review panel: lists every repository the configured PAT can access and, per
 * expanded repo, its open pull requests, letting the user start AI code reviews on
 * them. Each PR row expands inline to show the PR detail, a Start Review action, and
 * a table of every review run on that PR (status + outcome). A live status badge on
 * each row reflects the latest review.
 */
@Component({
  selector: 'app-code-review-panel',
  standalone: true,
  imports: [
    CommonModule,
    MatIconModule,
    MatButtonModule,
    MatProgressSpinnerModule,
    MatTooltipModule,
    MatPaginatorModule,
    RouterLink,
    HealthIndicatorComponent,
    InlineBannerComponent,
    PendingIssueProposalsComponent,
  ],
  templateUrl: './code-review-panel.component.html',
  styleUrl: './code-review-panel.component.scss',
})
export class CodeReviewPanelComponent implements OnInit, OnDestroy {
  private readonly api = inject(CodingTeamApiService);
  private readonly integrationsApi = inject(IntegrationsApiService);
  private readonly cdr = inject(ChangeDetectorRef);

  healthCheck = (): ReturnType<CodingTeamApiService['health']> => this.api.health();

  // GitHub integration state
  githubConfigured = false;
  loadingConfig = true;

  // Repository list — every repo the configured PAT can access. The PAT's own
  // authorization configuration is the source of truth; nothing is configured in Khala.
  repos: GitHubRepoItem[] = [];
  loadingRepos = false;
  reposLoaded = false;
  repoError: string | null = null;
  /** The expanded repo whose pull requests are shown; null when all repo rows are collapsed. */
  selectedRepo: GitHubRepoItem | null = null;

  // Pull-request list (scoped to the expanded repo)
  pulls: GitHubPullRequestItem[] = [];
  loadingPulls = false;
  pullsLoaded = false;
  // Top-level banner: errors loading the PR list only.
  pullError: string | null = null;
  // Per-PR "Start Review" failures, shown inside that PR's expanded panel so a
  // start error and a list-load error never clobber each other.
  reviewErrors = new Map<number, string>();

  // Client-side pagination over the fully-fetched PR array
  readonly PAGE_SIZE_OPTIONS = [10, 25, 50];
  pageSize = 10;
  pageIndex = 0;

  // Accordion: the number of the currently-expanded PR row, or null.
  expandedPrNumber: number | null = null;

  // All review runs per PR number, newest-first. Hydrated from the backend on
  // load and updated live by the per-job pollers below.
  reviews = new Map<number, PrReviewRecord[]>();

  // Reviews whose Start Review request is in flight (disables the button). Keyed by
  // `owner/repo#number`, NOT bare PR number: PR numbers collide across repositories, so a
  // bare-number key would let an in-flight start in one repo block the same-numbered PR in
  // another (and is why this set does NOT need clearing on repo switch).
  starting = new Set<string>();

  // "Latest wins" guards so a slow response from a superseded repo load can't overwrite
  // a newer one (rapid collapse/re-expand of the same repo, or a fast repo switch).
  private readonly pullsLoad = new LatestOnly();
  private readonly reviewsLoad = new LatestOnly();

  // Job ids whose "create issues" request is in flight (disables the button).
  creatingIssues = new Set<string>();
  // Per-review "create issues" failure, shown beneath that review's proposals.
  createIssueErrors = new Map<string, string>();

  // Live status pollers keyed by job id, so ngOnDestroy can tear them all down.
  private pollers = new Map<string, Subscription>();

  // Completes on destroy; every HTTP subscription is gated on it so a late
  // response can't update a torn-down component.
  private readonly destroy$ = new Subject<void>();

  ngOnInit(): void {
    this.checkGitHubConfig();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
    this.stopAllPollers();
  }

  checkGitHubConfig(): void {
    this.loadingConfig = true;
    this.integrationsApi
      .getGitHubConfig()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (cfg) => {
          // Repository access is defined by the PAT itself, so enabled + token is all
          // the page needs — no owner/repo configuration is required.
          this.githubConfigured = cfg.enabled && cfg.token_configured;
          this.loadingConfig = false;
          if (this.githubConfigured) {
            this.loadRepos();
          }
        },
        error: () => {
          this.githubConfigured = false;
          this.loadingConfig = false;
        },
      });
  }

  /**
   * Load every repository the PAT can access into the repo accordion. Resets any
   * expanded repo/PR state; on error `repoError` is surfaced.
   */
  loadRepos(): void {
    if (this.loadingRepos) return;
    this.loadingRepos = true;
    this.repoError = null;
    this.selectedRepo = null;
    this.resetRepoScopedState();
    this.integrationsApi
      .getGitHubRepos()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (repos) => {
          this.repos = repos;
          this.reposLoaded = true;
          this.loadingRepos = false;
        },
        error: (err: unknown) => {
          this.repoError = extractErrorDetail(err, 'Failed to load repositories.');
          this.loadingRepos = false;
        },
      });
  }

  /**
   * Toggle a repository row: expand it (and load its open PRs) when collapsed,
   * collapse it when it is the open one. Only one repo is expanded at a time.
   */
  toggleRepo(repo: GitHubRepoItem): void {
    if (this.selectedRepo?.full_name === repo.full_name) {
      this.selectedRepo = null;
      this.resetRepoScopedState();
      return;
    }
    this.selectedRepo = repo;
    this.resetRepoScopedState();
    this.loadPulls();
  }

  /**
   * Drop everything keyed by bare PR number when the expanded repo changes — PR numbers
   * collide across repositories, so records and errors from one repo must never render
   * under another's rows. (`starting` is NOT cleared here: it is keyed by `owner/repo#number`,
   * so it can't collide across repos, and clearing it would drop the in-flight double-submit
   * guard for a review still starting in the repo the user switches back to.)
   *
   * Pollers are disposed too, not left running: a poller mutates its record object,
   * but this clears the `reviews` map those records live in, so a surviving poller
   * would update an orphan nothing renders while a later hydrate rebuilds a *fresh*
   * record whose `startPolling` would no-op (the jobId still looked "polled"), freezing
   * the row. Disposing here means re-expanding the repo re-fetches history and attaches
   * a fresh poller to the shown record.
   */
  private resetRepoScopedState(): void {
    this.stopAllPollers();
    this.pulls = [];
    this.pullsLoaded = false;
    this.expandedPrNumber = null;
    this.pullError = null;
    this.reviews = new Map();
    this.reviewErrors.clear();
  }

  /** Load the expanded repo's open pull requests, then hydrate their review history. */
  loadPulls(): void {
    const repo = this.selectedRepo;
    if (!repo) return;
    // Claim a token so a slow response superseded by a newer load (collapse/re-expand of
    // the same repo, or a repo switch) is discarded — and the loading flag is always
    // cleared by the current handler, so it can't get stuck true after a switch-away.
    const token = this.pullsLoad.next();
    this.loadingPulls = true;
    this.pullError = null;
    this.expandedPrNumber = null;
    this.integrationsApi
      .getGitHubPullRequests({ owner: repo.owner, repo: repo.name })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (pulls) => {
          // Superseded by a newer load: that newer load owns the flag; drop this response.
          if (!this.pullsLoad.isCurrent(token)) return;
          // Current load — clear the flag even after a switch-away (else it sticks true);
          // only render + hydrate when still on this repo.
          this.loadingPulls = false;
          if (this.selectedRepo?.full_name !== repo.full_name) return;
          this.pulls = pulls;
          this.pageIndex = 0;
          this.pullsLoaded = true;
          this.hydrateReviews(repo);
        },
        error: (err: unknown) => {
          if (!this.pullsLoad.isCurrent(token)) return;
          this.loadingPulls = false;
          if (this.selectedRepo?.full_name !== repo.full_name) return;
          this.pullError = extractErrorDetail(err, 'Failed to load pull requests.');
        },
      });
  }

  /**
   * Reconcile the in-memory review map with the backend for the expanded repo. The
   * backend is the source of truth, but any review that still has a live poller is
   * preserved as the *same* object its poller mutates — so a review started while
   * this request was on the wire is never dropped and its poller is never killed
   * (closing a hydrate-vs-startReview race). Records from a *different* repository
   * are never folded in: PR numbers collide across repos, so the rebuilt map holds
   * this repo's reviews only.
   *
   * Note: this fetches the repository's recent reviews in one call (the backend
   * `limit`, default 500) rather than per-PR, because the row status badges need
   * the latest review for *every* listed PR up front; a per-PR-on-expand fetch
   * would leave the list without badges. Consequence of the cap: in a repo with
   * more than ~500 recent runs, the oldest runs (and the badges for the least
   * active PRs) may be absent until a future "latest run per PR" backend query
   * lands. Best-effort: a failure leaves the page usable without history.
   */
  private hydrateReviews(repo: GitHubRepoItem): void {
    const token = this.reviewsLoad.next();
    this.integrationsApi
      .getGitHubReviewHistory({ owner: repo.owner, repo: repo.name })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (items) => {
          // Drop if superseded by a newer repo load, or if the user is no longer on this
          // repo (a switch that didn't issue a new hydrate) — PR numbers collide across repos.
          if (!this.reviewsLoad.isCurrent(token) || this.selectedRepo?.full_name !== repo.full_name) return;
          // Records that still have a live poller must survive the rebuild as the
          // same object their poller writes to, or the UI stops updating.
          const live = new Map<string, PrReviewRecord>();
          for (const list of this.reviews.values()) {
            for (const record of list) {
              if (this.pollers.has(record.jobId)) live.set(record.jobId, record);
            }
          }
          const map = new Map<number, PrReviewRecord[]>();
          const seen = new Set<string>();
          for (const item of items) {
            // Prefer the live record so its poller keeps updating the shown object.
            const record = live.get(item.job_id) ?? this.toRecord(item, repo);
            seen.add(record.jobId);
            const list = map.get(record.prNumber) ?? [];
            list.push(record); // backend returns newest-first; preserve that order
            map.set(record.prNumber, list);
          }
          // Carry over any still-polling review the snapshot didn't include yet
          // (e.g. one started while this request was in flight) — but only when it
          // belongs to this repository, so a switched-away repo's run can't surface
          // under another repo's identical PR number.
          for (const [jobId, record] of live) {
            if (seen.has(jobId)) continue;
            if (record.owner !== repo.owner || record.repo !== repo.name) continue;
            const list = map.get(record.prNumber) ?? [];
            list.unshift(record); // newest-first
            map.set(record.prNumber, list);
          }
          this.reviews = map;
          for (const list of map.values()) {
            for (const record of list) {
              if (!isCodingTeamTerminalStatus(record.status) && !record.error) {
                this.startPolling(record); // guarded against double-start
              }
            }
          }
          this.cdr.markForCheck();
        },
        error: () => {
          // History is a best-effort enhancement; the PR list still works without it.
        },
      });
  }

  private toRecord(item: CodeReviewRunItem, repo: GitHubRepoItem): PrReviewRecord {
    const parsed = Date.parse(item.created_at);
    // completed_at is present only on terminal runs; an unparseable/absent value
    // leaves completedAt undefined so the row shows no duration.
    const completed = item.completed_at ? Date.parse(item.completed_at) : NaN;
    return {
      jobId: item.job_id,
      prNumber: item.pr_number,
      owner: repo.owner,
      repo: repo.name,
      startedAt: Number.isNaN(parsed) ? Date.now() : parsed,
      completedAt: Number.isNaN(completed) ? undefined : completed,
      status: item.status,
      statusText: item.status_text,
      reviewSummary: item.review_summary,
      prUrl: item.pr_url,
      error: item.error,
    };
  }

  /** The slice of PRs visible on the current page. */
  get pagedPulls(): GitHubPullRequestItem[] {
    const start = this.pageIndex * this.pageSize;
    return this.pulls.slice(start, start + this.pageSize);
  }

  /** Adopt a new page index/size from the paginator (the `pagedPulls` getter re-slices). */
  onPageChange(event: PageEvent): void {
    this.pageIndex = event.pageIndex;
    this.pageSize = event.pageSize;
  }

  /** Toggle the accordion expansion for a PR (only one open at a time). */
  togglePull(pull: GitHubPullRequestItem): void {
    this.expandedPrNumber = this.expandedPrNumber === pull.number ? null : pull.number;
  }

  /**
   * Repo-scoped key for the in-flight `starting` set (PR numbers collide across repos).
   *
   * Preconditions: `repo` is a repository item; `prNumber` is a PR number.
   * Postconditions: returns a stable `owner/repo#number` key, lowercased so it is
   * case-insensitive (GitHub treats owner/repo case-insensitively). Pure — no side effects.
   */
  private startKey(repo: GitHubRepoItem, prNumber: number): string {
    return `${repo.owner.toLowerCase()}/${repo.name.toLowerCase()}#${prNumber}`;
  }

  /**
   * Whether a Start Review request for this PR in the expanded repo is in flight.
   *
   * Preconditions: `pull` is a PR from the currently expanded repo's list.
   * Postconditions: returns true iff a repo is expanded and its `owner/repo#pull.number`
   * key is in `starting`; false when no repo is expanded. Pure — no side effects.
   */
  isStarting(pull: GitHubPullRequestItem): boolean {
    return !!this.selectedRepo && this.starting.has(this.startKey(this.selectedRepo, pull.number));
  }

  /** Start a code review on a PR in the expanded repo, recording it and polling it live. */
  startReview(pull: GitHubPullRequestItem): void {
    const repo = this.selectedRepo;
    if (!repo) return;
    const key = this.startKey(repo, pull.number);
    if (this.starting.has(key)) return;
    this.starting.add(key);
    this.reviewErrors.delete(pull.number);
    this.integrationsApi
      .runGitHubReviewPr({ pr_number: pull.number, owner: repo.owner, repo: repo.name })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (resp: RunPrReviewResponse) => {
          this.starting.delete(key);
          const record: PrReviewRecord = {
            jobId: resp.job_id,
            prNumber: pull.number,
            owner: repo.owner,
            repo: repo.name,
            startedAt: Date.now(),
            status: resp.status,
            prUrl: resp.pr_url,
          };
          // Only record AND poll while the user is still on the repo the review targeted.
          // If they switched away, don't spin an orphan poller for an off-screen record —
          // the hydrate on return re-fetches this run's history and attaches a fresh poller.
          if (this.selectedRepo?.full_name === repo.full_name) {
            const list = this.reviews.get(pull.number) ?? [];
            list.unshift(record); // newest-first
            this.reviews.set(pull.number, list);
            this.startPolling(record);
          }
          this.cdr.markForCheck();
        },
        error: (err: unknown) => {
          this.starting.delete(key);
          // Only surface the error while the user is still on the repo the review targeted;
          // reviewErrors is keyed by bare PR number, so an unguarded set would render this
          // failure under another repo's identically-numbered PR after a switch.
          if (this.selectedRepo?.full_name === repo.full_name) {
            this.reviewErrors.set(pull.number, extractErrorDetail(err, 'Failed to start review.'));
          }
          this.cdr.markForCheck();
        },
      });
  }

  /** The "Start Review" error for a PR, if its last attempt failed. */
  reviewErrorFor(prNumber: number): string | null {
    return this.reviewErrors.get(prNumber) ?? null;
  }

  /**
   * Begin polling a review job's status. Mutates `record` in place on each
   * update (and calls `markForCheck()` so the UI refreshes under any change
   * detection strategy). The subscription is registered in `pollers` for
   * teardown and is explicitly unsubscribed — and removed from `pollers` — once
   * the job reaches a terminal state or the connection is lost, so no poller
   * outlives the job. `ngOnDestroy` tears down any still-running pollers.
   */
  private startPolling(record: PrReviewRecord): void {
    if (this.pollers.has(record.jobId)) return;
    const sub = pollJobStatus(
      this.api,
      record.jobId,
      (status: CodingTeamJobStatus) => {
        // Mutate the record in place; markForCheck() makes the live badge/table
        // refresh independent of the change-detection strategy (safe under OnPush).
        record.status = status.status;
        record.statusText = status.status_text;
        record.reviewSummary = status.review_summary ?? record.reviewSummary;
        record.prUrl = status.github_pr_url ?? record.prUrl;
        record.error = status.error;
        if (isCodingTeamTerminalStatus(status.status)) {
          // Stamp the completion time when the run first goes terminal so the row can
          // show a duration without a reload. Prefer the server's timestamp for the
          // terminal update over the browser clock: a backgrounded tab, an offline
          // gap, or a delayed poll can push Date.now() well past when the job actually
          // finished, overstating the duration until a reload swaps in the persisted
          // completed_at. `??=` so a value from a prior hydrate is never overwritten.
          record.completedAt ??= this.terminalTimestamp(status);
          this.disposePoller(record.jobId);
        }
        this.cdr.markForCheck();
      },
      () => {
        record.error = 'Lost connection to the coding team — status polling failed.';
        this.disposePoller(record.jobId);
        this.cdr.markForCheck();
      },
    );
    this.pollers.set(record.jobId, sub);
  }

  /**
   * Best-effort completion time (ms since epoch) for a review whose live poll just
   * reached a terminal state.
   *
   * Preconditions: `status` is the terminal job-status payload for the review.
   * Postconditions: returns the server's terminal-update timestamp
   * (`last_activity_at`, else `updated_at`) parsed to ms when one is present and
   * valid; otherwise falls back to the browser clock (`Date.now()`). Preferring the
   * server time keeps the duration accurate when the browser observes the terminal
   * status late (backgrounded tab, offline gap, delayed poll). Reads the clock only
   * on the fallback path; otherwise pure.
   */
  private terminalTimestamp(status: CodingTeamJobStatus): number {
    const serverTs = status.last_activity_at ?? status.updated_at;
    if (serverTs) {
      const parsed = Date.parse(serverTs);
      if (!Number.isNaN(parsed)) return parsed;
    }
    return Date.now();
  }

  /** Unsubscribe a poller and drop it from the registry (idempotent). */
  private disposePoller(jobId: string): void {
    this.pollers.get(jobId)?.unsubscribe();
    this.pollers.delete(jobId);
  }

  private stopAllPollers(): void {
    for (const sub of this.pollers.values()) {
      sub.unsubscribe();
    }
    this.pollers.clear();
  }

  /** All review runs for a PR, newest-first. */
  reviewsFor(prNumber: number): PrReviewRecord[] {
    return this.reviews.get(prNumber) ?? [];
  }

  /** The most recent review run for a PR, or null. */
  latestReview(prNumber: number): PrReviewRecord | null {
    return this.reviewsFor(prNumber)[0] ?? null;
  }

  /** True when a PR has at least one recorded review run. */
  hasReviews(prNumber: number): boolean {
    return this.reviewsFor(prNumber).length > 0;
  }

  /** True when a PR's latest review is still running (drives the row spinner). */
  isLatestRunning(prNumber: number): boolean {
    const latest = this.latestReview(prNumber);
    return !!latest && !latest.error && !isCodingTeamTerminalStatus(latest.status);
  }

  /** True once a single review run has reached a terminal state. */
  isRecordTerminal(record: PrReviewRecord): boolean {
    return isCodingTeamTerminalStatus(record.status);
  }

  /**
   * Findings posted as standalone comments, normalized across the field rename.
   * Rows persisted before `body_findings` became `comment_findings` only carry the
   * legacy key, so fall back to it (then 0) rather than rendering a blank count.
   */
  commentFindings(summary: CodeReviewSummary): number {
    return summary.comment_findings ?? summary.body_findings ?? 0;
  }

  /**
   * Chip labels for a review's Findings cell (total / inline / standalone-comment counts).
   *
   * Preconditions: `summary` is a review summary.
   * Postconditions: returns exactly three labels, in order, derived from
   * `total_issues`, `inline_comments`, and `commentFindings(summary)` (which folds
   * in the legacy `body_findings` key). Pure — no side effects.
   */
  findingChips(summary: CodeReviewSummary): string[] {
    return [
      `${summary.total_issues} finding(s)`,
      `${summary.inline_comments} inline`,
      `${this.commentFindings(summary)} comments`,
    ];
  }

  /** Fixed critical→info ordering for the per-row severity metric chips. */
  private static readonly SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'info'] as const;

  /**
   * Human-readable elapsed time of a review run (e.g. "45s", "1m 23s", "2h 5m").
   *
   * Preconditions: `record` is a review record held by this component.
   * Postconditions: returns a formatted duration string when the run is terminal
   * and carries a `completedAt` no earlier than `startedAt`; otherwise null (the
   * template renders "—" for a running review or a record without timestamps).
   * Pure — no side effects.
   */
  reviewDuration(record: PrReviewRecord): string | null {
    if (!this.isRecordTerminal(record) || record.completedAt === undefined) return null;
    const ms = record.completedAt - record.startedAt;
    if (ms < 0) {
      // A terminal review that completed before it started signals clock skew between
      // the start and completion timestamps. Surface it rather than silently showing
      // "—", so the data anomaly is visible in the console.
      console.warn(`Negative review duration for job ${record.jobId} (${ms}ms); showing no duration.`);
      return null;
    }
    const totalSec = Math.floor(ms / 1000);
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  }

  /**
   * Non-zero severity counts of a review, in fixed critical→info order, for the
   * per-row severity chips.
   *
   * Preconditions: `summary` may be undefined (review still running / no summary).
   * Postconditions: returns one `{ level, count }` entry per severity whose count
   * is greater than zero; returns [] when there is no summary, no `severity_counts`,
   * or every level is zero. Pure — no side effects.
   */
  severityEntries(summary: CodeReviewSummary | undefined): { level: string; count: number }[] {
    const counts = summary?.severity_counts;
    if (!counts) return [];
    const entries: { level: string; count: number }[] = [];
    for (const level of CodeReviewPanelComponent.SEVERITY_ORDER) {
      const count = counts[level] ?? 0;
      if (count > 0) entries.push({ level, count });
    }
    return entries;
  }

  /** Row status badge text derived from the latest review, or null when none. */
  badgeLabel(prNumber: number): string | null {
    const latest = this.latestReview(prNumber);
    if (!latest) return null;
    if (latest.error) return 'error';
    if (isCodingTeamTerminalStatus(latest.status)) {
      return latest.reviewSummary?.event ?? latest.status;
    }
    return latest.status;
  }

  /** Row status badge CSS class derived from the latest review. */
  badgeClass(prNumber: number): string {
    const latest = this.latestReview(prNumber);
    if (!latest) return '';
    if (latest.error || latest.status === 'failed') return 'cr-job-status--failed';
    if (isCodingTeamTerminalStatus(latest.status)) return 'cr-job-status--completed';
    return '';
  }

  // --- Pre-existing-bug proposals -> GitHub issues -------------------------
  // Selection/formatting/rendering lives in PendingIssueProposalsComponent;
  // this component only gates whether it's shown and owns the actual API call.

  /**
   * True when a *terminal* review has pre-existing-bug proposals to show.
   * Gated on terminal so proposals never flash mid-review, before the summary
   * (and its proposal list) is final.
   */
  hasProposals(record: PrReviewRecord): boolean {
    return (
      this.isRecordTerminal(record) &&
      (record.reviewSummary?.pending_issue_proposals?.length ?? 0) > 0
    );
  }

  isCreatingIssues(jobId: string): boolean {
    return this.creatingIssues.has(jobId);
  }

  createIssueErrorFor(jobId: string): string | null {
    return this.createIssueErrors.get(jobId) ?? null;
  }

  /**
   * File GitHub issues for the given (child-selected) proposal ids of a
   * review. On success the record's proposal list is replaced with the
   * server's updated copy (filed proposals now carry `issue_url`) — the child
   * component reconciles its own selection against that fresh list.
   */
  createIssuesFor(record: PrReviewRecord, ids: string[]): void {
    const jobId = record.jobId;
    if (ids.length === 0 || this.creatingIssues.has(jobId)) return;
    this.creatingIssues.add(jobId);
    this.createIssueErrors.delete(jobId);
    this.integrationsApi
      .createGitHubReviewIssues(record.owner, record.repo, jobId, ids)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (resp) => {
          this.creatingIssues.delete(jobId);
          if (record.reviewSummary) {
            record.reviewSummary = {
              ...record.reviewSummary,
              pending_issue_proposals: resp.proposals,
            };
          }
          this.cdr.markForCheck();
        },
        error: (err: unknown) => {
          this.creatingIssues.delete(jobId);
          this.createIssueErrors.set(jobId, extractErrorDetail(err, 'Failed to create issue(s).'));
          this.cdr.markForCheck();
        },
      });
  }
}
