import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, Subject, throwError } from 'rxjs';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { vi } from 'vitest';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { CodeReviewDashboardComponent } from './code-review-dashboard.component';
import type { CodeReviewRunItem, GitHubConfigResponse, GitHubPullRequestItem, GitHubRepoItem } from '../../models/integrations.model';
import { makePulls, makeReviewRecord as record, REPO } from './testing/fixtures';

const CONFIGURED: GitHubConfigResponse = {
  enabled: true,
  token_configured: true,
  default_label: 'ai',
};

const UNCONFIGURED: GitHubConfigResponse = {
  enabled: false,
  token_configured: false,
  default_label: '',
};

describe('CodeReviewDashboardComponent', () => {
  let component: CodeReviewDashboardComponent;
  let fixture: ComponentFixture<CodeReviewDashboardComponent>;
  let apiSpy: {
    health: ReturnType<typeof vi.fn>;
    getJobStatus: ReturnType<typeof vi.fn>;
  };
  let integrationsSpy: {
    getGitHubConfig: ReturnType<typeof vi.fn>;
    getGitHubRepos: ReturnType<typeof vi.fn>;
    getGitHubPullRequests: ReturnType<typeof vi.fn>;
    runGitHubReviewPr: ReturnType<typeof vi.fn>;
    getGitHubReviewHistory: ReturnType<typeof vi.fn>;
    createGitHubReviewIssues: ReturnType<typeof vi.fn>;
  };

  async function setup(): Promise<void> {
    await TestBed.configureTestingModule({
      imports: [CodeReviewDashboardComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: CodingTeamApiService, useValue: apiSpy },
        { provide: IntegrationsApiService, useValue: integrationsSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(CodeReviewDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    // PRs are per-repo now: expand the first accessible repo (when any) so its open
    // PRs load, matching what most tests exercised before repo-scoped browsing.
    if (component.repos.length > 0) {
      component.toggleRepo(component.repos[0]);
      fixture.detectChanges();
    }
  }

  beforeEach(() => {
    vi.useFakeTimers();
    apiSpy = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      getJobStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running' })),
    };
    integrationsSpy = {
      getGitHubConfig: vi.fn().mockReturnValue(of(CONFIGURED)),
      getGitHubRepos: vi.fn().mockReturnValue(of([REPO])),
      getGitHubPullRequests: vi.fn().mockReturnValue(of(makePulls(3))),
      runGitHubReviewPr: vi.fn().mockReturnValue(
        of({ job_id: 'j1', pr_number: 1, pr_url: 'https://example.com/pull/1', status: 'pending', message: '' }),
      ),
      getGitHubReviewHistory: vi.fn().mockReturnValue(of([])),
      createGitHubReviewIssues: vi.fn(),
    };
  });

  afterEach(() => {
    fixture?.destroy();
    vi.useRealTimers();
  });

  // -------------------------------------------------------------------------
  // Config + list loading
  // -------------------------------------------------------------------------

  it('should create, list the accessible repos, and load the expanded repo\'s PRs + history', async () => {
    await setup();
    expect(component.githubConfigured).toBe(true);
    expect(integrationsSpy.getGitHubRepos).toHaveBeenCalled();
    expect(component.repos.length).toBe(1);
    // The expanded repo scopes both the PR list and the review history.
    expect(integrationsSpy.getGitHubPullRequests).toHaveBeenCalledWith({ owner: 'acme', repo: 'widgets' });
    expect(integrationsSpy.getGitHubReviewHistory).toHaveBeenCalledWith({ owner: 'acme', repo: 'widgets' });
    expect(component.pulls.length).toBe(3);
    expect(component.pullsLoaded).toBe(true);
  });

  it('renders the repo-list error banner in the DOM', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(
      throwError(() => ({ error: { detail: 'bad credentials' } })),
    );
    await setup();
    fixture.detectChanges();
    const banner = fixture.nativeElement.querySelector('app-inline-banner[variant="error"]');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('bad credentials');
  });

  it('does not load repos when GitHub is unconfigured', async () => {
    integrationsSpy.getGitHubConfig.mockReturnValue(of(UNCONFIGURED));
    await setup();
    expect(component.githubConfigured).toBe(false);
    expect(integrationsSpy.getGitHubRepos).not.toHaveBeenCalled();
    expect(integrationsSpy.getGitHubPullRequests).not.toHaveBeenCalled();
  });

  it('surfaces a repo-list load error', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(
      throwError(() => ({ error: { detail: 'bad credentials' } })),
    );
    await setup();
    expect(component.repoError).toBe('bad credentials');
    expect(component.loadingRepos).toBe(false);
    expect(integrationsSpy.getGitHubPullRequests).not.toHaveBeenCalled();
  });

  it('collapsing the expanded repo drops all repo-scoped state', async () => {
    await setup();
    component['reviewRuns']['_reviews'].set(1, [record()]);
    component.toggleRepo(component.repos[0]); // collapse
    expect(component.selectedRepo).toBeNull();
    expect(component.pulls.length).toBe(0);
    expect(component.pullsLoaded).toBe(false);
    expect(component['reviewRuns'].reviews.size).toBe(0);
  });

  it('treats a config check error as unconfigured', async () => {
    integrationsSpy.getGitHubConfig.mockReturnValue(throwError(() => new Error('boom')));
    await setup();
    expect(component.githubConfigured).toBe(false);
    expect(component.loadingConfig).toBe(false);
  });

  it('renders the no-repo-access empty state with an h3 heading (Group C)', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([]));
    await setup();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-empty-state h3')?.textContent).toContain('No repository access');
  });

  it('renders the unconfigured state with a working "Set up GitHub" CTA (Group C)', async () => {
    integrationsSpy.getGitHubConfig.mockReturnValue(of(UNCONFIGURED));
    await setup();
    fixture.detectChanges();
    const link = fixture.nativeElement.querySelector('app-empty-state a[routerLink="/integrations"]');
    expect(link?.textContent).toContain('Set up GitHub');
  });

  it('surfaces a load error from the PR list', async () => {
    integrationsSpy.getGitHubPullRequests.mockReturnValue(
      throwError(() => ({ error: { detail: 'rate limited' } })),
    );
    await setup();
    expect(component.pullError).toBe('rate limited');
    expect(component.loadingPulls).toBe(false);
  });

  it('ignores a concurrent loadRepos while one is already in flight', async () => {
    await setup();
    const slow = new Subject<GitHubRepoItem[]>();
    integrationsSpy.getGitHubRepos.mockClear();
    integrationsSpy.getGitHubRepos.mockReturnValue(slow.asObservable());
    component.loadRepos();
    component.loadRepos(); // guarded no-op — a second fetch must not be issued
    expect(integrationsSpy.getGitHubRepos).toHaveBeenCalledTimes(1);
    slow.next([REPO]);
    slow.complete();
    expect(component.loadingRepos).toBe(false);
  });

  it('discards a PR-list response that lands after the user switched repos', async () => {
    await setup();
    component.toggleRepo(component.repos[0]); // collapse so pulls start from an empty baseline
    const slow = new Subject<GitHubPullRequestItem[]>();
    integrationsSpy.getGitHubPullRequests.mockReturnValue(slow.asObservable());
    component.selectedRepo = REPO;
    component.loadPulls();
    // Switch to another repo while acme/widgets' PRs are on the wire.
    component.selectedRepo = { ...REPO, full_name: 'other/thing', owner: 'other', name: 'thing' };
    slow.next(makePulls(2));
    slow.complete();
    // The stale response must not render under the other repo's row, and the loading flag
    // must still be cleared (never stuck true after a switch-away).
    expect(component.pulls.length).toBe(0);
    expect(component.pullsLoaded).toBe(false);
    expect(component.loadingPulls).toBe(false);
  });

  it('paginates the PR list client-side', async () => {
    integrationsSpy.getGitHubPullRequests.mockReturnValue(of(makePulls(25)));
    await setup();
    expect(component.pagedPulls.length).toBe(10);
    component.onPageChange({ pageIndex: 1, pageSize: 10, length: 25 });
    expect(component.pageIndex).toBe(1);
    expect(component.pagedPulls[0].number).toBe(11);
  });

  it('handles pagination edge cases: page-size change, out-of-bounds page, empty list', async () => {
    integrationsSpy.getGitHubPullRequests.mockReturnValue(of(makePulls(25)));
    await setup();
    // Changing the page size re-slices the visible window from the new index.
    component.onPageChange({ pageIndex: 0, pageSize: 25, length: 25 });
    expect(component.pagedPulls.length).toBe(25);
    // A page index beyond the available items yields an empty slice, not a crash.
    component.onPageChange({ pageIndex: 9, pageSize: 10, length: 25 });
    expect(component.pagedPulls).toEqual([]);
    // An empty PR list always pages to an empty slice.
    component.pulls = [];
    component.onPageChange({ pageIndex: 0, pageSize: 10, length: 0 });
    expect(component.pagedPulls).toEqual([]);
  });

  // -------------------------------------------------------------------------
  // PR search/filter + hide drafts (Group E3)
  // -------------------------------------------------------------------------
  // makePulls alternates `draft: i % 2 === 0`, so odd-numbered PRs are drafts and
  // even-numbered PRs are not.

  it('filteredPulls returns every pull by default (empty filter, hideDrafts off)', async () => {
    await setup();
    expect(component.filteredPulls).toEqual(component.pulls);
  });

  it('filteredPulls matches case-insensitively on title', async () => {
    await setup();
    component.pullFilter = 'pr 2';
    expect(component.filteredPulls.map((p) => p.number)).toEqual([2]);
  });

  it('filteredPulls matches on #<number> and the bare number', async () => {
    await setup();
    component.pullFilter = '#2';
    expect(component.filteredPulls.map((p) => p.number)).toEqual([2]);
    component.pullFilter = '2';
    expect(component.filteredPulls.map((p) => p.number)).toEqual([2]);
  });

  it('hideDrafts excludes draft PRs', async () => {
    await setup();
    component.hideDrafts = true;
    expect(component.filteredPulls.map((p) => p.number)).toEqual([2]);
  });

  it('combines the text filter and hideDrafts', async () => {
    integrationsSpy.getGitHubPullRequests.mockReturnValue(of(makePulls(5)));
    await setup();
    component.hideDrafts = true;
    component.pullFilter = 'PR';
    expect(component.filteredPulls.map((p) => p.number)).toEqual([2, 4]);
  });

  it('onPullFilterChange resets pageIndex to 0', async () => {
    integrationsSpy.getGitHubPullRequests.mockReturnValue(of(makePulls(25)));
    await setup();
    component.onPageChange({ pageIndex: 1, pageSize: 10, length: 25 });
    expect(component.pageIndex).toBe(1);
    component.onPullFilterChange('PR');
    expect(component.pageIndex).toBe(0);
  });

  it('onHideDraftsChange resets pageIndex to 0', async () => {
    integrationsSpy.getGitHubPullRequests.mockReturnValue(of(makePulls(25)));
    await setup();
    component.onPageChange({ pageIndex: 1, pageSize: 10, length: 25 });
    expect(component.pageIndex).toBe(1);
    component.onHideDraftsChange(true);
    expect(component.pageIndex).toBe(0);
  });

  it('pagedPulls slices filteredPulls, not the raw pulls array', async () => {
    integrationsSpy.getGitHubPullRequests.mockReturnValue(of(makePulls(25)));
    await setup();
    component.hideDrafts = true; // 12 non-draft PRs remain: 2, 4, ..., 24
    component.pageSize = 10;
    component.pageIndex = 0;
    expect(component.pagedPulls.map((p) => p.number)).toEqual([2, 4, 6, 8, 10, 12, 14, 16, 18, 20]);
    component.pageIndex = 1;
    expect(component.pagedPulls.map((p) => p.number)).toEqual([22, 24]);
  });

  it('renders "No pull requests match" when the filter excludes everything', async () => {
    await setup();
    component.pullFilter = 'nonexistent-xyz';
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-empty-state h3')?.textContent).toContain('No pull requests match');
  });

  it('noPullMatchDescription names only the active filter(s)', async () => {
    await setup();
    component.pullFilter = 'nonexistent';
    expect(component.noPullMatchDescription).toBe('Try a different search.');
    component.pullFilter = '';
    component.hideDrafts = true;
    expect(component.noPullMatchDescription).toBe('Turn off Hide drafts to see more.');
    component.pullFilter = 'nonexistent';
    expect(component.noPullMatchDescription).toBe('Try a different search or turn off Hide drafts.');
  });

  it('renders the tailored empty-state description when the PR filter excludes everything', async () => {
    await setup();
    component.pullFilter = 'nonexistent-xyz';
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-empty-state .kh-empty-message')?.textContent).toContain('Try a different search.');
  });

  it('pullCountAnnouncement is empty when the repo has no open PRs', async () => {
    integrationsSpy.getGitHubPullRequests.mockReturnValue(of([]));
    await setup();
    expect(component.pullCountAnnouncement).toBe('');
  });

  it('pullCountAnnouncement is singular for one result and plural otherwise, and updates as the filter changes', async () => {
    await setup(); // default mock: makePulls(3)
    expect(component.pullCountAnnouncement).toBe('3 pull requests shown');
    component.pullFilter = '#2';
    expect(component.pullCountAnnouncement).toBe('1 pull request shown');
  });

  it('renders the PR count announcement in a role="status" live region', async () => {
    await setup();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const texts = Array.from(el.querySelectorAll('[role="status"]')).map((r) => r.textContent?.trim());
    expect(texts).toContain('3 pull requests shown');
  });

  it("the rendered paginator's length reflects filteredPulls.length once filtered", async () => {
    integrationsSpy.getGitHubPullRequests.mockReturnValue(of(makePulls(25)));
    await setup();
    component.hideDrafts = true; // 12 non-draft PRs remain, still above the pagination threshold
    fixture.detectChanges();
    const paginatorDebug = fixture.debugElement.query(By.css('mat-paginator'));
    expect(paginatorDebug).not.toBeNull();
    expect(paginatorDebug.componentInstance.length).toBe(12);
  });

  it('collapsing/switching repos clears pullFilter but leaves hideDrafts untouched', async () => {
    await setup();
    component.pullFilter = 'something';
    component.hideDrafts = true;
    component.toggleRepo(component.repos[0]); // collapse
    expect(component.pullFilter).toBe('');
    expect(component.hideDrafts).toBe(true);
  });

  it('renders the PR-list error banner inside the expanded repo panel', async () => {
    integrationsSpy.getGitHubPullRequests.mockReturnValue(
      throwError(() => ({ error: { detail: 'rate limited' } })),
    );
    await setup();
    fixture.detectChanges();
    const panel = fixture.nativeElement.querySelector('.cr-repo-pulls');
    const banner = panel?.querySelector('app-inline-banner[variant="error"]');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('rate limited');
  });

  it('survives a review-history load failure without breaking the PR list', async () => {
    integrationsSpy.getGitHubReviewHistory.mockReturnValue(throwError(() => new Error('nope')));
    await setup();
    expect(component.pullsLoaded).toBe(true);
    expect(component['reviewRuns'].reviews.size).toBe(0);
  });

  // -------------------------------------------------------------------------
  // Repo search/filter (Group E1)
  // -------------------------------------------------------------------------

  const GIZMOS: GitHubRepoItem = { ...REPO, full_name: 'acme/gizmos', name: 'gizmos', description: 'Gizmo tooling' };

  it('filteredRepos returns every repo when repoFilter is empty', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, GIZMOS]));
    await setup();
    expect(component.filteredRepos).toEqual([REPO, GIZMOS]);
  });

  it('filteredRepos matches case-insensitively on full_name', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, GIZMOS]));
    await setup();
    component.repoFilter = 'GIZMOS';
    expect(component.filteredRepos).toEqual([GIZMOS]);
  });

  it('filteredRepos matches case-insensitively on description', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, GIZMOS]));
    await setup();
    component.repoFilter = 'WIDGET';
    expect(component.filteredRepos).toEqual([REPO]);
  });

  it('filteredRepos is empty when nothing matches', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, GIZMOS]));
    await setup();
    component.repoFilter = 'nonexistent';
    expect(component.filteredRepos).toEqual([]);
  });

  it('typing in the repo filter narrows the rendered repo rows', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, GIZMOS]));
    await setup();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelectorAll('.cr-repo-row').length).toBe(2);
    component.repoFilter = 'gizmos';
    fixture.detectChanges();
    expect(el.querySelectorAll('.cr-repo-row').length).toBe(1);
  });

  it('renders "No repositories match" (not "No repository access") when the filter empties the list', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, GIZMOS]));
    await setup();
    component.repoFilter = 'nonexistent';
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-empty-state h3')?.textContent).toContain('No repositories match');
    expect(el.textContent).not.toContain('No repository access');
  });

  it('repoCountAnnouncement is empty when the token has no repository access', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([]));
    await setup();
    expect(component.repoCountAnnouncement).toBe('');
  });

  it('repoCountAnnouncement is singular for one result and plural otherwise, and updates as the filter changes', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, GIZMOS]));
    await setup();
    expect(component.repoCountAnnouncement).toBe('2 repositories shown');
    component.repoFilter = 'gizmos';
    expect(component.repoCountAnnouncement).toBe('1 repository shown');
  });

  it('renders the repo count announcement in a role="status" live region', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, GIZMOS]));
    await setup();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const texts = Array.from(el.querySelectorAll('[role="status"]')).map((r) => r.textContent?.trim());
    expect(texts).toContain('2 repositories shown');
  });

  it('onRepoFilterChange updates repoFilter', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, GIZMOS]));
    await setup();
    component.onRepoFilterChange('gizmos');
    expect(component.repoFilter).toBe('gizmos');
  });

  it('onRepoFilterChange collapses the expanded repo when the new filter excludes it', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, GIZMOS]));
    await setup(); // setup() expands repos[0] = REPO (acme/widgets)
    expect(component.selectedRepo?.full_name).toBe('acme/widgets');
    component.onRepoFilterChange('gizmos'); // excludes acme/widgets
    expect(component.selectedRepo).toBeNull();
    expect(component.pulls.length).toBe(0);
    expect(component.pullsLoaded).toBe(false);
  });

  it('onRepoFilterChange leaves the expanded repo untouched when it still matches', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, GIZMOS]));
    await setup();
    expect(component.selectedRepo?.full_name).toBe('acme/widgets');
    component.onRepoFilterChange('widgets'); // still matches acme/widgets
    expect(component.selectedRepo?.full_name).toBe('acme/widgets');
    expect(component.pullsLoaded).toBe(true);
  });

  it('onRepoFilterChange is a no-op collapse when no repo is expanded', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, GIZMOS]));
    await setup();
    component.toggleRepo(component.repos[0]); // collapse
    expect(component.selectedRepo).toBeNull();
    component.onRepoFilterChange('nonexistent');
    expect(component.selectedRepo).toBeNull();
  });

  it('typing a repo filter that excludes the expanded repo collapses its PR panel in the DOM', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, GIZMOS]));
    await setup();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.cr-repo-pulls')).not.toBeNull();
    component.onRepoFilterChange('gizmos');
    fixture.detectChanges();
    expect(el.querySelector('.cr-repo-pulls')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Visible repo description (Group E2)
  // -------------------------------------------------------------------------

  it("renders a repo's description as visible text under its name", async () => {
    await setup();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.cr-repo-row__description')?.textContent?.trim()).toBe(REPO.description);
  });

  it('renders no description element for a repo with no description', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([{ ...REPO, description: '' }]));
    await setup();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.cr-repo-row__description')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Accordion expand/collapse
  // -------------------------------------------------------------------------

  it('expands and collapses a pull request (accordion, one open at a time)', async () => {
    await setup();
    const [a, b] = component.pulls;
    component.togglePull(a);
    expect(component.expandedPrNumber).toBe(a.number);
    // Expanding b replaces a (only one open).
    component.togglePull(b);
    expect(component.expandedPrNumber).toBe(b.number);
    // Re-clicking the open row collapses it.
    component.togglePull(b);
    expect(component.expandedPrNumber).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Teardown
  // -------------------------------------------------------------------------
  // The review-run domain (hydration, polling, starting reviews, badge derivation,
  // issue creation) is owned by PrReviewRunsService and covered by its own spec
  // (pr-review-runs.service.spec.ts); the template binds to `reviewRuns` directly for
  // all of it, so this component holds no review-run wrapper methods of its own. This
  // test instead covers the one invariant that spans both classes: destroying this
  // component must stop the service's pollers, since the service is only torn down
  // because Angular destroys it as a provider of this component.

  it('stops all live polling when the component is destroyed', async () => {
    await setup();
    apiSpy.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'running' })); // stays non-terminal
    component['reviewRuns'].startReview(component.pulls[0]);
    expect(component['reviewRuns']['pollers'].size).toBe(1);

    const callsBefore = apiSpy.getJobStatus.mock.calls.length;
    fixture.destroy();
    expect(component['reviewRuns']['pollers'].size).toBe(0);
    vi.advanceTimersByTime(15000);
    expect(apiSpy.getJobStatus.mock.calls.length).toBe(callsBefore);
  });

  // -------------------------------------------------------------------------
  // Review-completion live region (Group B2)
  // -------------------------------------------------------------------------

  it('mirrors reviewRuns.announce$ into reviewAnnouncement for the live-region binding', async () => {
    await setup();
    expect(component.reviewAnnouncement).toBe('');
    component['reviewRuns'].announce$.next('Review for pull request #1 completed.');
    // Cleared synchronously, then set on the next tick (see the repeated-announcement test).
    expect(component.reviewAnnouncement).toBe('');
    vi.advanceTimersByTime(0);
    expect(component.reviewAnnouncement).toBe('Review for pull request #1 completed.');
  });

  it('re-announces an identical outcome by passing through empty first, so the DOM text always changes', async () => {
    await setup();
    component['reviewRuns'].announce$.next('Review for pull request #1 completed.');
    vi.advanceTimersByTime(0);
    expect(component.reviewAnnouncement).toBe('Review for pull request #1 completed.');
    // A second, identical outcome (e.g. re-running the review) must still clear first —
    // otherwise Angular's interpolation sees no string change and never touches the DOM,
    // so assistive tech would not re-announce it.
    component['reviewRuns'].announce$.next('Review for pull request #1 completed.');
    expect(component.reviewAnnouncement).toBe('');
    vi.advanceTimersByTime(0);
    expect(component.reviewAnnouncement).toBe('Review for pull request #1 completed.');
  });

  it('stops mirroring announce$ into reviewAnnouncement once the component is destroyed', async () => {
    await setup();
    fixture.destroy();
    component['reviewRuns'].announce$.next('Review for pull request #1 completed.');
    expect(component.reviewAnnouncement).toBe('');
  });

  it('does not land a deferred announcement that was still pending when the component was destroyed', async () => {
    await setup();
    component['reviewRuns'].announce$.next('Review for pull request #1 completed.');
    expect(component.reviewAnnouncement).toBe(''); // cleared, deferred write not yet run
    fixture.destroy();
    vi.advanceTimersByTime(0);
    expect(component.reviewAnnouncement).toBe('');
  });

  // -------------------------------------------------------------------------
  // Full-stack DOM integration (component + real PrReviewRunsService + child)
  // -------------------------------------------------------------------------

  it('renders an expanded PR detail with a reviews table', async () => {
    integrationsSpy.getGitHubReviewHistory.mockReturnValue(
      of([
        {
          job_id: 'r1',
          pr_number: 1,
          pr_url: 'https://example.com/pull/1',
          status: 'completed',
          review_summary: { total_issues: 3, inline_comments: 2, comment_findings: 1, event: 'REQUEST_CHANGES' },
          created_at: '2026-02-01T00:00:00Z',
        },
      ] as CodeReviewRunItem[]),
    );
    await setup();
    component.togglePull(component.pulls[0]);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.cr-pull-detail')).toBeTruthy();
    expect(el.querySelectorAll('.cr-reviews-table tbody tr').length).toBe(1);
    // Collapsing removes the detail panel.
    component.togglePull(component.pulls[0]);
    fixture.detectChanges();
    expect(el.querySelector('.cr-pull-detail')).toBeNull();
  });

  it('renders a review-runs announcement in the visually-hidden status live region', async () => {
    await setup();
    component['reviewRuns'].announce$.next('Review for pull request #1 completed.');
    vi.advanceTimersByTime(0);
    fixture.detectChanges();
    const region = fixture.nativeElement.querySelector('[role="status"]');
    expect(region?.textContent?.trim()).toBe('Review for pull request #1 completed.');
  });

  it('updates the rendered row badge + table as a live poll completes', async () => {
    await setup();
    apiSpy.getJobStatus.mockReturnValue(
      of({
        job_id: 'j1',
        status: 'completed',
        review_summary: { total_issues: 2, inline_comments: 1, comment_findings: 1, event: 'REQUEST_CHANGES' },
      }),
    );
    const el: HTMLElement = fixture.nativeElement;

    component['reviewRuns'].startReview(component.pulls[0]);
    component.togglePull(component.pulls[0]);
    fixture.detectChanges();
    // Before the first poll the row badge shows the initial (pending) status, as its
    // friendly label (Group E4) rather than the raw wire value.
    expect(el.querySelector('.cr-row-badge')?.textContent).toContain('Starting…');

    vi.advanceTimersByTime(5000); // one poll tick -> completed
    fixture.detectChanges();
    // The row badge now reflects the terminal outcome (friendly text) and the table row updated.
    expect(el.querySelector('.cr-row-badge')?.textContent).toContain('Changes requested');
    const statusCell = el.querySelector('.cr-reviews-table tbody tr td');
    expect(statusCell?.textContent).toContain('completed');
  });

  it("exposes the badge's full friendly status via aria-label (Group E4)", async () => {
    await setup();
    apiSpy.getJobStatus.mockReturnValue(
      of({
        job_id: 'j1',
        status: 'completed',
        review_summary: { total_issues: 2, inline_comments: 1, comment_findings: 1, event: 'REQUEST_CHANGES' },
      }),
    );
    const el: HTMLElement = fixture.nativeElement;
    component['reviewRuns'].startReview(component.pulls[0]);
    fixture.detectChanges();
    expect(el.querySelector('.cr-row-badge')?.getAttribute('aria-label')).toBe('Starting…');

    vi.advanceTimersByTime(5000); // one poll tick -> completed
    fixture.detectChanges();
    expect(el.querySelector('.cr-row-badge')?.getAttribute('aria-label')).toBe('Changes requested');
  });

  it('shows the spinner (not the status icon) while running, and the icon once terminal (Group E4)', async () => {
    await setup();
    apiSpy.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'running' })); // stays non-terminal
    const el: HTMLElement = fixture.nativeElement;
    component['reviewRuns'].startReview(component.pulls[0]);
    fixture.detectChanges();
    vi.advanceTimersByTime(5000); // one poll tick, still running
    fixture.detectChanges();
    let badge = el.querySelector('.cr-row-badge');
    expect(badge?.querySelector('mat-spinner')).not.toBeNull();
    expect(badge?.querySelector('.cr-row-badge__icon')).toBeNull();

    apiSpy.getJobStatus.mockReturnValue(
      of({
        job_id: 'j1',
        status: 'completed',
        review_summary: { total_issues: 0, inline_comments: 0, comment_findings: 0, event: 'APPROVE' },
      }),
    );
    vi.advanceTimersByTime(5000); // next poll tick -> completed
    fixture.detectChanges();
    badge = el.querySelector('.cr-row-badge');
    expect(badge?.querySelector('mat-spinner')).toBeNull();
    const icon = badge?.querySelector('.cr-row-badge__icon');
    expect(icon?.getAttribute('aria-hidden')).toBe('true');
    expect(icon?.textContent?.trim()).toBe('check_circle');
  });

  it('renders the per-PR start-review error inside the expanded detail', async () => {
    integrationsSpy.runGitHubReviewPr.mockReturnValue(
      throwError(() => ({ error: { detail: 'no such PR' } })),
    );
    await setup();
    component.togglePull(component.pulls[0]);
    component['reviewRuns'].startReview(component.pulls[0]);
    fixture.detectChanges();
    const detail = fixture.nativeElement.querySelector('.cr-pull-detail');
    expect(detail?.textContent).toContain('no such PR');
  });

  it('leaves the PR-list banner untouched when a start-review request fails', async () => {
    integrationsSpy.runGitHubReviewPr.mockReturnValue(
      throwError(() => ({ error: { detail: 'no such PR' } })),
    );
    await setup();
    component['reviewRuns'].startReview(component.pulls[0]);
    // A per-PR start-review failure is a PrReviewRunsService concern (reviewErrorFor);
    // it must never surface through the component's own list-load banner.
    expect(component.pullError).toBeNull();
  });
});
