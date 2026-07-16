import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatPaginatorModule, PageEvent } from '@angular/material/paginator';
import { RouterLink } from '@angular/router';
import { Subject } from 'rxjs';
import { takeUntil } from 'rxjs/operators';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { PrReviewDetailComponent } from './pr-review-detail/pr-review-detail.component';
import { PrReviewRunsService } from './pr-review-runs.service';
import type { GitHubPullRequestItem, GitHubRepoItem } from '../../models/integrations.model';
import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import { extractErrorDetail } from '../../shared/extract-error-detail';
import { LatestOnly } from '../../shared/latest-only';
import type { PrReviewRecord } from './pr-review-record.model';

// Re-exported so existing importers of `PrReviewRecord` from this module keep working;
// the interface now lives in ./pr-review-record.model so both this panel and its
// extracted detail child can depend on it without a component-to-component import cycle.
export type { PrReviewRecord } from './pr-review-record.model';

/**
 * Code Review panel: lists every repository the configured PAT can access and, per
 * expanded repo, its open pull requests, letting the user start AI code reviews on
 * them. Each PR row expands inline to show the PR detail, a Start Review action, and
 * a table of every review run on that PR (status + outcome). A live status badge on
 * each row reflects the latest review.
 *
 * This component owns repo/PR-list browsing and pagination; the review-run domain
 * (hydration, live polling, starting reviews, filing issues from proposals, and the
 * row status badge derivation) is owned by the injected `PrReviewRunsService` — see
 * that service for the contract of everything this component delegates to it.
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
    PrReviewDetailComponent,
  ],
  providers: [PrReviewRunsService],
  templateUrl: './code-review-panel.component.html',
  styleUrl: './code-review-panel.component.scss',
})
export class CodeReviewPanelComponent implements OnInit, OnDestroy {
  private readonly api = inject(CodingTeamApiService);
  private readonly integrationsApi = inject(IntegrationsApiService);
  private readonly reviewRuns = inject(PrReviewRunsService);

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

  // Client-side pagination over the fully-fetched PR array
  readonly PAGE_SIZE_OPTIONS = [10, 25, 50];
  pageSize = 10;
  pageIndex = 0;

  // Accordion: the number of the currently-expanded PR row, or null.
  expandedPrNumber: number | null = null;

  // "Latest wins" guard so a slow response from a superseded repo load can't overwrite
  // a newer one (rapid collapse/re-expand of the same repo, or a fast repo switch).
  private readonly pullsLoad = new LatestOnly();

  // Completes on destroy; every HTTP subscription is gated on it so a late
  // response can't update a torn-down component.
  private readonly destroy$ = new Subject<void>();

  ngOnInit(): void {
    this.checkGitHubConfig();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
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
   * Drop the PR list/pagination state when the expanded repo changes, and hand the
   * review-run domain (reviews, pollers, errors) to `PrReviewRunsService.reset` so it
   * can apply the same repo-scoping rules on its own state.
   */
  private resetRepoScopedState(): void {
    this.pulls = [];
    this.pullsLoaded = false;
    this.expandedPrNumber = null;
    this.pullError = null;
    this.reviewRuns.reset(this.selectedRepo);
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
          this.reviewRuns.hydrate(repo);
        },
        error: (err: unknown) => {
          if (!this.pullsLoad.isCurrent(token)) return;
          this.loadingPulls = false;
          if (this.selectedRepo?.full_name !== repo.full_name) return;
          this.pullError = extractErrorDetail(err, 'Failed to load pull requests.');
        },
      });
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

  // --- Delegates to PrReviewRunsService -------------------------------------
  // The template binds these exact names; each forwards to the service, passing
  // `selectedRepo` through where the service needs to know which repo is active.

  /** Reviews whose Start Review request is in flight (disables the button). */
  get creatingIssues(): Set<string> {
    return this.reviewRuns.creatingIssues;
  }

  /** Per-review "create issues" failure, shown beneath that review's proposals. */
  get createIssueErrors(): Map<string, string> {
    return this.reviewRuns.createIssueErrors;
  }

  /** All review runs per PR number, newest-first (exposed for direct test/debug access). */
  get reviews(): Map<number, PrReviewRecord[]> {
    return this.reviewRuns.reviews;
  }

  isStarting(pull: GitHubPullRequestItem): boolean {
    return this.reviewRuns.isStarting(this.selectedRepo, pull.number);
  }

  startReview(pull: GitHubPullRequestItem): void {
    if (!this.selectedRepo) return;
    this.reviewRuns.startReview(this.selectedRepo, pull);
  }

  reviewErrorFor(prNumber: number): string | null {
    return this.reviewRuns.reviewErrorFor(prNumber);
  }

  reviewsFor(prNumber: number): PrReviewRecord[] {
    return this.reviewRuns.reviewsFor(prNumber);
  }

  latestReview(prNumber: number): PrReviewRecord | null {
    return this.reviewRuns.latestReview(prNumber);
  }

  hasReviews(prNumber: number): boolean {
    return this.reviewRuns.hasReviews(prNumber);
  }

  isLatestRunning(prNumber: number): boolean {
    return this.reviewRuns.isLatestRunning(prNumber);
  }

  badgeLabel(prNumber: number): string | null {
    return this.reviewRuns.badgeLabel(prNumber);
  }

  badgeClass(prNumber: number): string {
    return this.reviewRuns.badgeClass(prNumber);
  }

  createIssuesFor(record: PrReviewRecord, ids: string[]): void {
    this.reviewRuns.createIssuesFor(record, ids);
  }
}
