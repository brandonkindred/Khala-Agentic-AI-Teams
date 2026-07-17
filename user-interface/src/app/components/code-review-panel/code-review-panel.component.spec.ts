import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, Subject, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { vi } from 'vitest';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { CodeReviewPanelComponent } from './code-review-panel.component';
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

describe('CodeReviewPanelComponent', () => {
  let component: CodeReviewPanelComponent;
  let fixture: ComponentFixture<CodeReviewPanelComponent>;
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
      imports: [CodeReviewPanelComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: CodingTeamApiService, useValue: apiSpy },
        { provide: IntegrationsApiService, useValue: integrationsSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(CodeReviewPanelComponent);
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
    // Before the first poll the row badge shows the initial (pending) status.
    expect(el.querySelector('.cr-row-badge')?.textContent).toContain('pending');

    vi.advanceTimersByTime(5000); // one poll tick -> completed
    fixture.detectChanges();
    // The row badge now reflects the terminal outcome and the table row updated.
    expect(el.querySelector('.cr-row-badge')?.textContent).toContain('REQUEST_CHANGES');
    const statusCell = el.querySelector('.cr-reviews-table tbody tr td');
    expect(statusCell?.textContent).toContain('completed');
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
