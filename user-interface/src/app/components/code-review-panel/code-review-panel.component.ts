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
import type {
  CodeReviewRunItem,
  GitHubPullRequestItem,
  RunPrReviewResponse,
} from '../../models/integrations.model';
import type { CodeReviewSummary, CodingTeamJobStatus } from '../../models/coding-team.model';
import { isCodingTeamTerminalStatus } from '../../models/job-status.model';

/**
 * One code-review run on a pull request. Held in memory and kept live by a
 * per-job poller; the authoritative copy is persisted backend-side (the
 * `code_review_runs` table) and re-hydrated on load so history survives reloads.
 */
export interface PrReviewRecord {
  jobId: string;
  prNumber: number;
  /** Milliseconds since epoch when the review started (for the table timestamp). */
  startedAt: number;
  status: string;
  statusText?: string;
  reviewSummary?: CodeReviewSummary;
  prUrl?: string;
  error?: string;
}

/**
 * Code Review panel: lists open pull requests from the configured GitHub repo and
 * lets the user start AI code reviews on them. Each PR row expands inline to show
 * the PR detail, a Start Review action, and a table of every review run on that PR
 * (status + outcome). A live status badge on each row reflects the latest review.
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
  githubOwner = '';
  githubRepo = '';
  loadingConfig = true;

  // Pull-request list
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

  // PR numbers whose Start Review request is in flight (disables the button).
  starting = new Set<number>();

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
          this.githubConfigured = cfg.enabled && cfg.token_configured && !!cfg.owner && !!cfg.repo;
          this.githubOwner = cfg.owner;
          this.githubRepo = cfg.repo;
          this.loadingConfig = false;
          if (this.githubConfigured) {
            this.loadPulls();
          }
        },
        error: () => {
          this.githubConfigured = false;
          this.loadingConfig = false;
        },
      });
  }

  loadPulls(): void {
    this.loadingPulls = true;
    this.pullError = null;
    this.expandedPrNumber = null;
    this.integrationsApi
      .getGitHubPullRequests()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (pulls) => {
          this.pulls = pulls;
          this.pageIndex = 0;
          this.pullsLoaded = true;
          this.loadingPulls = false;
          this.hydrateReviews();
        },
        error: (err: { error?: { detail?: string }; message?: string }) => {
          this.pullError = err?.error?.detail || err?.message || 'Failed to load pull requests.';
          this.loadingPulls = false;
        },
      });
  }

  /**
   * Reconcile the in-memory review map with the backend. The backend is the
   * source of truth, but any review that still has a live poller is preserved as
   * the *same* object its poller mutates — so a review started while this request
   * was on the wire is never dropped and its poller is never killed (closing a
   * hydrate-vs-startReview race).
   *
   * Note: this fetches the repository's recent reviews in one call (bounded by
   * the backend `limit`) rather than per-PR, because the row status badges need
   * the latest review for *every* listed PR up front; a per-PR-on-expand fetch
   * would leave the list without badges. Best-effort: a failure leaves the page
   * usable without history.
   */
  private hydrateReviews(): void {
    this.integrationsApi
      .getGitHubReviewHistory()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (items) => {
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
            const record = live.get(item.job_id) ?? this.toRecord(item);
            seen.add(record.jobId);
            const list = map.get(record.prNumber) ?? [];
            list.push(record); // backend returns newest-first; preserve that order
            map.set(record.prNumber, list);
          }
          // Carry over any still-polling review the snapshot didn't include yet
          // (e.g. one started while this request was in flight).
          for (const [jobId, record] of live) {
            if (seen.has(jobId)) continue;
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

  private toRecord(item: CodeReviewRunItem): PrReviewRecord {
    const parsed = Date.parse(item.created_at);
    return {
      jobId: item.job_id,
      prNumber: item.pr_number,
      startedAt: Number.isNaN(parsed) ? Date.now() : parsed,
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

  onPageChange(event: PageEvent): void {
    this.pageIndex = event.pageIndex;
    this.pageSize = event.pageSize;
  }

  /** Toggle the accordion expansion for a PR (only one open at a time). */
  togglePull(pull: GitHubPullRequestItem): void {
    this.expandedPrNumber = this.expandedPrNumber === pull.number ? null : pull.number;
  }

  /** Start a code review on a PR, recording it and polling it live. */
  startReview(pull: GitHubPullRequestItem): void {
    if (this.starting.has(pull.number)) return;
    this.starting.add(pull.number);
    this.reviewErrors.delete(pull.number);
    this.integrationsApi
      .runGitHubReviewPr({ pr_number: pull.number })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (resp: RunPrReviewResponse) => {
          this.starting.delete(pull.number);
          const record: PrReviewRecord = {
            jobId: resp.job_id,
            prNumber: pull.number,
            startedAt: Date.now(),
            status: resp.status,
            prUrl: resp.pr_url,
          };
          const list = this.reviews.get(pull.number) ?? [];
          list.unshift(record); // newest-first
          this.reviews.set(pull.number, list);
          this.startPolling(record);
        },
        error: (err: { error?: { detail?: string }; message?: string }) => {
          this.starting.delete(pull.number);
          this.reviewErrors.set(
            pull.number,
            err?.error?.detail || err?.message || 'Failed to start review.',
          );
        },
      });
  }

  /** The "Start Review" error for a PR, if its last attempt failed. */
  reviewErrorFor(prNumber: number): string | null {
    return this.reviewErrors.get(prNumber) ?? null;
  }

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
          this.pollers.delete(record.jobId);
        }
        this.cdr.markForCheck();
      },
      () => {
        record.error = 'Lost connection to the coding team — status polling failed.';
        this.pollers.delete(record.jobId);
        this.cdr.markForCheck();
      },
    );
    this.pollers.set(record.jobId, sub);
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
}
