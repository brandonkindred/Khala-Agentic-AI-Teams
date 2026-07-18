import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatPaginatorModule, PageEvent } from '@angular/material/paginator';
import { RouterLink } from '@angular/router';
import { Subject, timer } from 'rxjs';
import { takeUntil } from 'rxjs/operators';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { PrReviewDetailComponent } from './pr-review-detail/pr-review-detail.component';
import { PrReviewRunsService } from './pr-review-runs.service';
import { badgeIcon, friendlyBadgeLabel } from './review-metrics';
import type { GitHubPullRequestItem, GitHubRepoItem } from '../../models/integrations.model';
import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { EmptyStateComponent } from '../../shared/empty-state/empty-state.component';
import { extractErrorDetail } from '../../shared/extract-error-detail';
import { LatestOnly } from '../../shared/latest-only';

/**
 * Code Review dashboard: lists every repository the configured PAT can access and, per
 * expanded repo, its open pull requests, letting the user start AI code reviews on
 * them. Each PR row expands inline to show the PR detail, a Start Review action, and
 * a table of every review run on that PR (status + outcome). A live status badge on
 * each row reflects the latest review.
 *
 * This component owns repo/PR-list browsing and pagination only; the review-run domain
 * (hydration, live polling, starting reviews, filing issues from proposals, and the row
 * status badge derivation) is owned by the injected `PrReviewRunsService`, and the
 * template binds directly to `reviewRuns` for all of it — this class holds no
 * review-run wrapper methods, with one exception: `reviewAnnouncement` mirrors
 * `reviewRuns.announce$` into a plain field purely because a template can bind to a
 * field but cannot subscribe to an Observable directly. See `PrReviewRunsService` for
 * that contract.
 */
@Component({
  selector: 'app-code-review-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatIconModule,
    MatButtonModule,
    MatFormFieldModule,
    MatInputModule,
    MatProgressSpinnerModule,
    MatTooltipModule,
    MatPaginatorModule,
    RouterLink,
    HealthIndicatorComponent,
    InlineBannerComponent,
    LoadingSpinnerComponent,
    EmptyStateComponent,
    PrReviewDetailComponent,
  ],
  providers: [PrReviewRunsService],
  templateUrl: './code-review-dashboard.component.html',
  styleUrl: './code-review-dashboard.component.scss',
})
export class CodeReviewDashboardComponent implements OnInit, OnDestroy {
  private readonly api = inject(CodingTeamApiService);
  private readonly integrationsApi = inject(IntegrationsApiService);
  /** Exposed (not private) so the template can bind to it directly, e.g. `reviewRuns.badgeLabel(...)`. */
  protected readonly reviewRuns = inject(PrReviewRunsService);

  // Friendly badge text/icon are pure functions in `review-metrics.ts` (unit-tested
  // there in isolation), applied to `reviewRuns.badgeLabel(...)`'s raw output. Exposed
  // as fields so the template calls them unchanged.
  readonly friendlyBadgeLabel = friendlyBadgeLabel;
  readonly badgeIcon = badgeIcon;

  /**
   * Latest review completion/failure/connection-lost sentence for the visually-hidden
   * `role="status"` live region in the template, mirrored from `reviewRuns.announce$`
   * (see `ngOnInit`) since a template can bind a plain field but not subscribe to an
   * Observable directly. Empty until the first announcement of this component instance;
   * stops updating once `destroy$` fires (the subscription below is gated on it).
   */
  reviewAnnouncement = '';

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
  /** Case-insensitive text narrowing `repos` to `filteredRepos`; empty shows every repo. */
  repoFilter = '';

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
    this.reviewRuns.announce$.pipe(takeUntil(this.destroy$)).subscribe((message) => {
      // Clear first so two announcements with identical text (e.g. two reviews on the
      // same PR both completing) still produce a real DOM text change on the next tick —
      // Angular's interpolation is a no-op when the string is unchanged, and assistive
      // tech only re-announces a role="status" region when its text actually mutates.
      // The deferred write is gated on the same destroy$ (rather than a raw setTimeout)
      // so a pending write can't land on an already-destroyed component.
      this.reviewAnnouncement = '';
      timer(0)
        .pipe(takeUntil(this.destroy$))
        .subscribe(() => {
          this.reviewAnnouncement = message;
        });
    });
  }

  /**
   * Preconditions: none.
   * Postconditions: `destroy$` is completed, so any subscription still gated on it via
   * `takeUntil` unsubscribes. (The injected `PrReviewRunsService`'s own `ngOnDestroy` —
   * called automatically by Angular since it is provided in this component's own
   * `providers` array — tears down its pollers independently.)
   */
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
          this.reviewRuns.hydrate();
        },
        error: (err: unknown) => {
          if (!this.pullsLoad.isCurrent(token)) return;
          this.loadingPulls = false;
          if (this.selectedRepo?.full_name !== repo.full_name) return;
          this.pullError = extractErrorDetail(err, 'Failed to load pull requests.');
        },
      });
  }

  /** `repos` narrowed by `repoFilter`, matching case-insensitively on `full_name` or `description`. */
  get filteredRepos(): GitHubRepoItem[] {
    const q = this.repoFilter.trim().toLowerCase();
    if (!q) return this.repos;
    return this.repos.filter(
      (r) => r.full_name.toLowerCase().includes(q) || (r.description ?? '').toLowerCase().includes(q),
    );
  }

  /** Polite live-region text for how many repos `filteredRepos` currently shows; empty
   *  before repos have loaded or when the token has no repo access at all. */
  get repoCountAnnouncement(): string {
    if (!this.reposLoaded || this.repos.length === 0) return '';
    const n = this.filteredRepos.length;
    return n === 1 ? '1 repository shown' : `${n} repositories shown`;
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
}
