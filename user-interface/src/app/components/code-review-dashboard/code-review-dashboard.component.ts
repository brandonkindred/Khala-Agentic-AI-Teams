import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
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
import { resultCountAnnouncement } from '../../shared/result-count-announcement';

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
    MatSlideToggleModule,
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
  /** Case-insensitive text narrowing `pulls` to `filteredPulls` (matches `#<number>` or title). */
  pullFilter = '';
  /** When true, `filteredPulls` excludes draft PRs. Persists across repo switches (a
   *  cross-repo preference, like `pageSize` already does) — only `pullFilter` is repo-scoped. */
  hideDrafts = false;

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
    this.pullFilter = '';
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

  /**
   * `repos` narrowed by `repoFilter`, matching case-insensitively on `full_name` or
   * `description`.
   *
   * Preconditions: none.
   * Postconditions: returns `repos` itself (same reference) when `repoFilter` is empty
   * or whitespace-only; otherwise a new array of the `repos` entries whose `full_name`
   * or `description` contains `repoFilter`, case-insensitively. Does not mutate `repos`.
   */
  get filteredRepos(): GitHubRepoItem[] {
    const q = this.repoFilter.trim().toLowerCase();
    if (!q) return this.repos;
    return this.repos.filter(
      (r) => r.full_name.toLowerCase().includes(q) || (r.description ?? '').toLowerCase().includes(q),
    );
  }

  /**
   * Polite live-region text for how many repos `filteredRepos` currently shows.
   *
   * Preconditions: none.
   * Postconditions: returns `''` before `repos` has loaded or when the token has no
   * repo access at all (`repos.length === 0`) — in both cases the search UI isn't
   * rendered, so there's nothing to announce. Otherwise returns
   * `resultCountAnnouncement(filteredRepos.length, 'repository', 'repositories')`.
   */
  get repoCountAnnouncement(): string {
    if (!this.reposLoaded || this.repos.length === 0) return '';
    return resultCountAnnouncement(this.filteredRepos.length, 'repository', 'repositories');
  }

  /**
   * Update the repo text filter, collapsing the expanded repo if the new filter
   * excludes it from `filteredRepos` — an expanded row that's no longer in the visible
   * list shouldn't stay silently expanded off-screen.
   *
   * Preconditions: `value` is the search input's current text.
   * Postconditions: `repoFilter` is set to `value`. If `selectedRepo` is set and is no
   * longer present in `filteredRepos` under the new filter, `selectedRepo` is cleared
   * and repo-scoped PR state is reset via `resetRepoScopedState` — identical to
   * collapsing that repo through `toggleRepo`. Otherwise `selectedRepo` and PR state
   * are left untouched.
   */
  onRepoFilterChange(value: string): void {
    this.repoFilter = value;
    const selected = this.selectedRepo;
    if (selected && !this.filteredRepos.some((r) => r.full_name === selected.full_name)) {
      this.selectedRepo = null;
      this.resetRepoScopedState();
    }
  }

  /**
   * `pulls` narrowed by `pullFilter` (substring match against `"#<number> <title>"`,
   * case-insensitively) and, when `hideDrafts` is set, with draft PRs excluded.
   *
   * Preconditions: none.
   * Postconditions: returns `pulls` itself (same reference) when `hideDrafts` is false
   * and `pullFilter` is empty or whitespace-only; otherwise a new array of the `pulls`
   * entries that pass both the draft exclusion (when `hideDrafts`) and the text match
   * (when `pullFilter` is non-empty). Does not mutate `pulls`.
   */
  get filteredPulls(): GitHubPullRequestItem[] {
    const q = this.pullFilter.trim().toLowerCase();
    if (!this.hideDrafts && !q) return this.pulls;
    return this.pulls.filter((p) => {
      if (this.hideDrafts && p.draft) return false;
      if (!q) return true;
      return `#${p.number} ${p.title}`.toLowerCase().includes(q);
    });
  }

  /**
   * Polite live-region text for how many PRs `filteredPulls` currently shows.
   *
   * Preconditions: none.
   * Postconditions: returns `''` before `pulls` has loaded or when the expanded repo
   * has no open PRs at all (`pulls.length === 0`) — in both cases the search UI isn't
   * rendered, so there's nothing to announce. Otherwise returns
   * `resultCountAnnouncement(filteredPulls.length, 'pull request', 'pull requests')`.
   */
  get pullCountAnnouncement(): string {
    if (!this.pullsLoaded || this.pulls.length === 0) return '';
    return resultCountAnnouncement(this.filteredPulls.length, 'pull request', 'pull requests');
  }

  /**
   * Tailored copy for the "No pull requests match" empty state, naming only the
   * filter(s) actually active so the suggested remedy is never irrelevant.
   *
   * Preconditions: called only while `filteredPulls.length === 0` and
   * `pulls.length > 0` (i.e. at least one of `pullFilter`/`hideDrafts` is narrowing the
   * list — otherwise `filteredPulls` would equal `pulls`, per its own postcondition).
   * Postconditions: returns copy mentioning "Hide drafts" only when `hideDrafts` is
   * true, and mentions searching only when `pullFilter` is non-empty (mentions both
   * when both are active).
   */
  get noPullMatchDescription(): string {
    const hasQuery = this.pullFilter.trim().length > 0;
    if (hasQuery && this.hideDrafts) return 'Try a different search or turn off Hide drafts.';
    if (this.hideDrafts) return 'Turn off Hide drafts to see more.';
    return 'Try a different search.';
  }

  /** The slice of PRs visible on the current page. */
  get pagedPulls(): GitHubPullRequestItem[] {
    const start = this.pageIndex * this.pageSize;
    return this.filteredPulls.slice(start, start + this.pageSize);
  }

  /** Adopt a new page index/size from the paginator (the `pagedPulls` getter re-slices). */
  onPageChange(event: PageEvent): void {
    this.pageIndex = event.pageIndex;
    this.pageSize = event.pageSize;
  }

  /**
   * Update the PR text filter and reset to page 1 — the current page may no longer
   * exist against the narrowed result set.
   *
   * Preconditions: `value` is the search input's current text.
   * Postconditions: `pullFilter` is set to `value` and `pageIndex` is set to 0.
   */
  onPullFilterChange(value: string): void {
    this.pullFilter = value;
    this.pageIndex = 0;
  }

  /**
   * Toggle whether `filteredPulls` excludes draft PRs, resetting to page 1.
   *
   * Preconditions: `value` is the toggle's new checked state.
   * Postconditions: `hideDrafts` is set to `value` and `pageIndex` is set to 0.
   */
  onHideDraftsChange(value: boolean): void {
    this.hideDrafts = value;
    this.pageIndex = 0;
  }

  /** Toggle the accordion expansion for a PR (only one open at a time). */
  togglePull(pull: GitHubPullRequestItem): void {
    this.expandedPrNumber = this.expandedPrNumber === pull.number ? null : pull.number;
  }
}
