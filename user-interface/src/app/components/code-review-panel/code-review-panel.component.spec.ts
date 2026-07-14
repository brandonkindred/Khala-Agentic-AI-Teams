import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, Subject, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { vi } from 'vitest';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { CodeReviewPanelComponent, PrReviewRecord } from './code-review-panel.component';
import type { PendingIssueProposal } from '../../models/coding-team.model';
import type {
  CodeReviewRunItem,
  GitHubConfigResponse,
  GitHubPullRequestItem,
  GitHubRepoItem,
  RunPrReviewResponse,
} from '../../models/integrations.model';

function makePulls(count: number): GitHubPullRequestItem[] {
  return Array.from({ length: count }, (_, i) => ({
    number: i + 1,
    title: `PR ${i + 1}`,
    body_preview: `body ${i + 1}`,
    author: 'octocat',
    html_url: `https://example.com/pull/${i + 1}`,
    head: `feature-${i + 1}`,
    base: 'main',
    draft: i % 2 === 0,
    labels: i % 2 === 0 ? ['needs-review'] : [],
    updated_at: '2026-01-01T00:00:00Z',
  }));
}

function record(over: Partial<PrReviewRecord> = {}): PrReviewRecord {
  return {
    jobId: 'j1',
    prNumber: 1,
    owner: 'acme',
    repo: 'widgets',
    startedAt: Date.parse('2026-01-01T00:00:00Z'),
    status: 'running',
    ...over,
  };
}

/** The repo the fake PAT can access; the panel lists repos and loads PRs per repo. */
const REPO: GitHubRepoItem = {
  owner: 'acme',
  name: 'widgets',
  full_name: 'acme/widgets',
  private: false,
  archived: false,
  html_url: 'https://github.com/acme/widgets',
  description: 'Widget factory',
  default_branch: 'main',
  open_issues_count: 3,
  pushed_at: '2026-06-09T10:00:00Z',
};

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
    component.reviews.set(1, [record()]);
    component.toggleRepo(component.repos[0]); // collapse
    expect(component.selectedRepo).toBeNull();
    expect(component.pulls.length).toBe(0);
    expect(component.pullsLoaded).toBe(false);
    expect(component.reviews.size).toBe(0);
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
  // Hydration from the backend
  // -------------------------------------------------------------------------

  it('hydrates the reviews map from history and resumes pollers for non-terminal runs', async () => {
    const items: CodeReviewRunItem[] = [
      {
        job_id: 'done-1',
        pr_number: 1,
        pr_url: 'https://example.com/pull/1',
        status: 'completed',
        review_summary: { total_issues: 1, inline_comments: 1, comment_findings: 0, event: 'COMMENT' },
        created_at: '2026-02-01T00:00:00Z',
      },
      {
        job_id: 'live-1',
        pr_number: 1,
        status: 'running',
        created_at: 'not-a-real-date', // exercises the invalid-timestamp fallback
      },
      {
        job_id: 'done-2',
        pr_number: 2,
        status: 'completed',
        created_at: '2026-02-02T00:00:00Z',
      },
    ];
    integrationsSpy.getGitHubReviewHistory.mockReturnValue(of(items));
    await setup();

    expect(component.reviewsFor(1).map((r) => r.jobId)).toEqual(['done-1', 'live-1']);
    expect(component.reviewsFor(2).map((r) => r.jobId)).toEqual(['done-2']);
    // The running run keeps polling; the two completed runs do not.
    expect(component['pollers'].has('live-1')).toBe(true);
    expect(component['pollers'].has('done-1')).toBe(false);
    // The invalid created_at fell back to a real timestamp.
    expect(Number.isNaN(component.reviewsFor(1)[1].startedAt)).toBe(false);
  });

  it('survives a review-history load failure', async () => {
    integrationsSpy.getGitHubReviewHistory.mockReturnValue(throwError(() => new Error('nope')));
    await setup();
    expect(component.pullsLoaded).toBe(true);
    expect(component.reviews.size).toBe(0);
  });

  it('keeps an in-flight review that a concurrent hydrate snapshot omits', async () => {
    await setup();
    // A hydrate is in flight (response deferred) when the user starts a review.
    const hydrate$ = new Subject<CodeReviewRunItem[]>();
    integrationsSpy.getGitHubReviewHistory.mockReturnValue(hydrate$.asObservable());
    apiSpy.getJobStatus.mockReturnValue(of({ job_id: 'A', status: 'running' }));
    integrationsSpy.runGitHubReviewPr.mockReturnValue(
      of({ job_id: 'A', pr_number: 1, pr_url: 'u', status: 'pending', message: '' }),
    );

    component.loadPulls(); // hydrate request fired, not yet resolved
    component.startReview(component.pulls[0]); // record A + live poller A
    expect(component.reviewsFor(1).map((r) => r.jobId)).toContain('A');
    expect(component['pollers'].has('A')).toBe(true);

    // The snapshot (taken before A was persisted) arrives without A.
    hydrate$.next([]);
    expect(component.reviewsFor(1).map((r) => r.jobId)).toContain('A');
    expect(component['pollers'].has('A')).toBe(true);
  });

  it('prefers the live in-flight record over a stale hydrate snapshot copy', async () => {
    await setup();
    const hydrate$ = new Subject<CodeReviewRunItem[]>();
    integrationsSpy.getGitHubReviewHistory.mockReturnValue(hydrate$.asObservable());
    apiSpy.getJobStatus.mockReturnValue(of({ job_id: 'A', status: 'running', status_text: 'live' }));
    integrationsSpy.runGitHubReviewPr.mockReturnValue(
      of({ job_id: 'A', pr_number: 1, pr_url: 'u', status: 'pending', message: '' }),
    );

    component.loadPulls();
    component.startReview(component.pulls[0]);
    vi.advanceTimersByTime(5000); // poll advances A to running + status_text 'live'
    const liveRec = component.reviewsFor(1).find((r) => r.jobId === 'A')!;

    // The snapshot includes A but with stale 'pending' status.
    hydrate$.next([{ job_id: 'A', pr_number: 1, status: 'pending', created_at: '2026-01-01T00:00:00Z' }]);

    const afterRec = component.reviewsFor(1).find((r) => r.jobId === 'A')!;
    expect(afterRec).toBe(liveRec); // same object, not a fresh snapshot copy
    expect(afterRec.statusText).toBe('live'); // live state retained
    expect(component['pollers'].has('A')).toBe(true);
  });

  // -------------------------------------------------------------------------
  // Starting reviews + polling
  // -------------------------------------------------------------------------

  it('starts a review, records it, and polls it live', async () => {
    await setup();
    apiSpy.getJobStatus.mockReturnValue(
      of({
        job_id: 'j1',
        status: 'completed',
        status_text: 'done',
        github_pr_url: 'https://example.com/pull/1',
        review_summary: { total_issues: 2, inline_comments: 1, comment_findings: 1, event: 'REQUEST_CHANGES' },
      }),
    );
    component.startReview(component.pulls[0]);
    // The expanded repo is the review target — repository access comes from the PAT.
    expect(integrationsSpy.runGitHubReviewPr).toHaveBeenCalledWith({ pr_number: 1, owner: 'acme', repo: 'widgets' });
    expect(component.reviewsFor(1).length).toBe(1);
    expect(component.reviewsFor(1)[0].jobId).toBe('j1');

    vi.advanceTimersByTime(5000);
    const rec = component.reviewsFor(1)[0];
    expect(rec.status).toBe('completed');
    expect(rec.reviewSummary?.event).toBe('REQUEST_CHANGES');
    expect(rec.prUrl).toBe('https://example.com/pull/1');
    // Terminal status removes the poller.
    expect(component['pollers'].has('j1')).toBe(false);
  });

  it('stops polling once a review reaches a terminal status', async () => {
    await setup();
    apiSpy.getJobStatus.mockReturnValue(
      of({
        job_id: 'j1',
        status: 'completed',
        review_summary: { total_issues: 0, inline_comments: 0, comment_findings: 0, event: 'APPROVE' },
      }),
    );
    component.startReview(component.pulls[0]);
    vi.advanceTimersByTime(5000); // first poll -> terminal -> poller disposed
    expect(component['pollers'].has('j1')).toBe(false);
    // No further polling after the job is terminal (subscription was torn down).
    const callsAfterTerminal = apiSpy.getJobStatus.mock.calls.length;
    vi.advanceTimersByTime(20000);
    expect(apiSpy.getJobStatus.mock.calls.length).toBe(callsAfterTerminal);
  });

  it('ignores a second startReview while one is already starting', async () => {
    await setup();
    const slow = new Subject<never>();
    integrationsSpy.runGitHubReviewPr.mockReturnValue(slow.asObservable());
    component.startReview(component.pulls[0]);
    component.startReview(component.pulls[0]); // second call ignored while the first is in flight
    expect(integrationsSpy.runGitHubReviewPr).toHaveBeenCalledTimes(1);
    expect(component.isStarting(component.pulls[0])).toBe(true);
  });

  it('surfaces a start-review error per PR without touching the list-load banner', async () => {
    integrationsSpy.runGitHubReviewPr.mockReturnValue(
      throwError(() => ({ error: { detail: 'no such PR' } })),
    );
    await setup();
    component.startReview(component.pulls[0]);
    expect(component.reviewErrorFor(1)).toBe('no such PR');
    expect(component.reviewErrorFor(2)).toBeNull();
    expect(component.pullError).toBeNull(); // list-load banner untouched
    expect(component.isStarting(component.pulls[0])).toBe(false);
  });

  it('an in-flight start on one repo does not block the same-numbered PR in another repo', async () => {
    await setup(); // acme/widgets expanded, PR #1 present
    const slow = new Subject<never>();
    integrationsSpy.runGitHubReviewPr.mockReturnValue(slow.asObservable());
    component.startReview(component.pulls[0]); // start acme/widgets PR #1 (in flight)
    // Switch to another repo that also has a PR #1.
    const other: GitHubRepoItem = { ...REPO, full_name: 'other/thing', owner: 'other', name: 'thing' };
    component.repos = [REPO, other];
    component.toggleRepo(other);
    fixture.detectChanges();
    // `starting` is keyed by owner/repo#number, so other/thing PR #1 is NOT considered starting.
    expect(component.isStarting(component.pulls[0])).toBe(false);
  });

  it('falls back to err.message when a start-review error has no detail', async () => {
    integrationsSpy.runGitHubReviewPr.mockReturnValue(throwError(() => ({ message: 'Network down' })));
    await setup();
    component.startReview(component.pulls[0]);
    expect(component.reviewErrorFor(1)).toBe('Network down');
  });

  it('does not surface a start-review failure from a switched-away repo under the new repo', async () => {
    await setup(); // acme/widgets expanded, PRs loaded (PR #1 present)
    const slow = new Subject<never>();
    integrationsSpy.runGitHubReviewPr.mockReturnValue(slow.asObservable());
    component.startReview(component.pulls[0]); // start on acme/widgets PR #1 (request pending)
    // Switch to a different repo that also has a PR #1 before the start resolves.
    const other: GitHubRepoItem = { ...REPO, full_name: 'other/thing', owner: 'other', name: 'thing' };
    component.repos = [REPO, other];
    component.toggleRepo(other);
    fixture.detectChanges();
    // acme/widgets' start now fails — reviewErrors is keyed by bare PR number, so an
    // unguarded set would render this failure under other/thing's PR #1.
    slow.error({ error: { detail: 'clone failed' } });
    expect(component.reviewErrorFor(1)).toBeNull();
  });

  it('does not spin a poller for a review that resolves after a switch-away', async () => {
    await setup(); // acme/widgets expanded, PR #1 present
    const slow = new Subject<RunPrReviewResponse>();
    integrationsSpy.runGitHubReviewPr.mockReturnValue(slow.asObservable());
    apiSpy.getJobStatus.mockClear();
    component.startReview(component.pulls[0]); // start acme/widgets PR #1 (pending)
    // Switch to another repo before the start resolves.
    const other: GitHubRepoItem = { ...REPO, full_name: 'other/thing', owner: 'other', name: 'thing' };
    component.repos = [REPO, other];
    component.toggleRepo(other);
    // The start now resolves while the user is on another repo — no record is shown and no
    // orphan poller must be attached (startPolling is inside the same-repo guard).
    slow.next({ job_id: 'jX', pr_number: 1, pr_url: 'u', status: 'pending', message: '' });
    slow.complete();
    vi.advanceTimersByTime(20000);
    expect(apiSpy.getJobStatus).not.toHaveBeenCalledWith('jX');
  });

  it('falls back to a default message when a start-review error has no detail or message', async () => {
    integrationsSpy.runGitHubReviewPr.mockReturnValue(throwError(() => ({})));
    await setup();
    component.startReview(component.pulls[0]);
    expect(component.reviewErrorFor(1)).toBe('Failed to start review.');
  });

  it('accumulates multiple reviews on the same PR, newest-first', async () => {
    await setup();
    integrationsSpy.runGitHubReviewPr
      .mockReturnValueOnce(of({ job_id: 'j1', pr_number: 1, pr_url: 'u1', status: 'pending', message: '' }))
      .mockReturnValueOnce(of({ job_id: 'j2', pr_number: 1, pr_url: 'u2', status: 'pending', message: '' }));
    component.startReview(component.pulls[0]);
    component.startReview(component.pulls[0]);
    expect(component.reviewsFor(1).map((r) => r.jobId)).toEqual(['j2', 'j1']);
    expect(component.hasReviews(1)).toBe(true);
  });

  it('polls concurrent reviews on different PRs without cross-talk', async () => {
    await setup();
    integrationsSpy.runGitHubReviewPr
      .mockReturnValueOnce(of({ job_id: 'ja', pr_number: 1, pr_url: 'u1', status: 'pending', message: '' }))
      .mockReturnValueOnce(of({ job_id: 'jb', pr_number: 2, pr_url: 'u2', status: 'pending', message: '' }));
    apiSpy.getJobStatus.mockImplementation((id: string) =>
      of({
        job_id: id,
        status: 'completed',
        review_summary: {
          total_issues: 0,
          inline_comments: 0,
          comment_findings: 0,
          event: id === 'ja' ? 'APPROVE' : 'COMMENT',
        },
      }),
    );
    component.startReview(component.pulls[0]);
    component.startReview(component.pulls[1]);
    vi.advanceTimersByTime(5000);

    expect(apiSpy.getJobStatus).toHaveBeenCalledWith('ja');
    expect(apiSpy.getJobStatus).toHaveBeenCalledWith('jb');
    expect(component.reviewsFor(1)[0].reviewSummary?.event).toBe('APPROVE');
    expect(component.reviewsFor(2)[0].reviewSummary?.event).toBe('COMMENT');
  });

  it('marks the record errored when polling loses the connection', async () => {
    await setup();
    apiSpy.getJobStatus.mockReturnValue(throwError(() => new Error('down')));
    component.startReview(component.pulls[0]);
    // Three consecutive failed polls trip the connection-lost handler.
    vi.advanceTimersByTime(15001);
    expect(component.reviewsFor(1)[0].error).toContain('Lost connection');
    expect(component['pollers'].has('j1')).toBe(false);
  });

  // -------------------------------------------------------------------------
  // Derived helpers (row badge + status)
  // -------------------------------------------------------------------------

  it('derives the row badge from the latest review run', async () => {
    await setup();
    expect(component.badgeLabel(1)).toBeNull();
    expect(component.badgeClass(1)).toBe('');

    component.reviews.set(1, [record({ status: 'running' })]);
    expect(component.badgeLabel(1)).toBe('running');
    expect(component.badgeClass(1)).toBe('');
    expect(component.isLatestRunning(1)).toBe(true);

    component.reviews.set(1, [
      record({ status: 'completed', reviewSummary: { total_issues: 0, inline_comments: 0, comment_findings: 0, event: 'COMMENT' } }),
    ]);
    expect(component.badgeLabel(1)).toBe('COMMENT');
    expect(component.badgeClass(1)).toBe('cr-job-status--completed');
    expect(component.isLatestRunning(1)).toBe(false);

    component.reviews.set(1, [record({ status: 'completed' })]); // terminal, no summary
    expect(component.badgeLabel(1)).toBe('completed');

    component.reviews.set(1, [record({ status: 'failed' })]);
    expect(component.badgeLabel(1)).toBe('failed');
    expect(component.badgeClass(1)).toBe('cr-job-status--failed');

    component.reviews.set(1, [record({ status: 'running', error: 'Lost connection' })]);
    expect(component.badgeLabel(1)).toBe('error');
    expect(component.badgeClass(1)).toBe('cr-job-status--failed');
    expect(component.isLatestRunning(1)).toBe(false);
  });

  it('normalizes the standalone-comment count across the body_findings rename', async () => {
    await setup();
    // New rows carry comment_findings.
    expect(
      component.commentFindings({ total_issues: 3, inline_comments: 1, comment_findings: 2, event: 'COMMENT' }),
    ).toBe(2);
    // Rows persisted before the rename only carry the legacy body_findings.
    expect(
      component.commentFindings({
        total_issues: 3,
        inline_comments: 1,
        body_findings: 2,
        event: 'COMMENT',
      }),
    ).toBe(2);
    // Neither present → 0 rather than a blank count.
    expect(
      component.commentFindings({ total_issues: 0, inline_comments: 0, event: 'COMMENT' }),
    ).toBe(0);
  });

  it('reports per-record terminality', async () => {
    await setup();
    expect(component.isRecordTerminal(record({ status: 'running' }))).toBe(false);
    expect(component.isRecordTerminal(record({ status: 'completed' }))).toBe(true);
  });

  // -------------------------------------------------------------------------
  // Teardown
  // -------------------------------------------------------------------------

  it('tears down all pollers on destroy', async () => {
    await setup();
    integrationsSpy.runGitHubReviewPr
      .mockReturnValueOnce(of({ job_id: 'jx', pr_number: 1, pr_url: 'u', status: 'pending', message: '' }))
      .mockReturnValueOnce(of({ job_id: 'jy', pr_number: 2, pr_url: 'u', status: 'pending', message: '' }));
    apiSpy.getJobStatus.mockReturnValue(of({ job_id: 'jx', status: 'running' })); // stays non-terminal
    component.startReview(component.pulls[0]);
    component.startReview(component.pulls[1]);
    expect(component['pollers'].size).toBe(2);

    const callsBefore = apiSpy.getJobStatus.mock.calls.length;
    component.ngOnDestroy();
    expect(component['pollers'].size).toBe(0);
    vi.advanceTimersByTime(15000);
    expect(apiSpy.getJobStatus.mock.calls.length).toBe(callsBefore);
  });

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

    component.startReview(component.pulls[0]);
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
    component.startReview(component.pulls[0]);
    fixture.detectChanges();
    const detail = fixture.nativeElement.querySelector('.cr-pull-detail');
    expect(detail?.textContent).toContain('no such PR');
  });


  // -------------------------------------------------------------------------
  // Pre-existing-bug proposals -> GitHub issues
  // -------------------------------------------------------------------------

  function proposal(id: string, over: Record<string, unknown> = {}): PendingIssueProposal {
    return {
      id,
      severity: 'high',
      category: 'logic',
      file_path: 'a.py',
      line: 3,
      description: `bug ${id}`,
      suggestion: 'fix',
      issue_number: null,
      issue_url: null,
      ...over,
    };
  }

  function terminalRecordWith(proposals: PendingIssueProposal[]): PrReviewRecord {
    return record({
      status: 'completed',
      reviewSummary: {
        total_issues: 0,
        inline_comments: 0,
        event: 'COMMENT',
        pending_issue_proposals: proposals,
      },
    });
  }

  it('exposes proposals only for terminal reviews', async () => {
    await setup();
    const running = record({ status: 'running', reviewSummary: {
      total_issues: 0, inline_comments: 0, event: 'COMMENT',
      pending_issue_proposals: [proposal('p0')],
    } });
    expect(component.hasProposals(running)).toBe(false);
    const done = terminalRecordWith([proposal('p0')]);
    expect(component.hasProposals(done)).toBe(true);
    expect(component.proposalsFor(done).length).toBe(1);
    expect(component.openProposals(done).length).toBe(1);
  });

  it('formats a proposal location (path:line, path, or empty)', async () => {
    await setup();
    expect(component.proposalLocation(proposal('p0'))).toBe('a.py:3');
    expect(component.proposalLocation(proposal('p0', { line: null }))).toBe('a.py');
    expect(component.proposalLocation(proposal('p0', { file_path: '', line: null }))).toBe('');
  });

  it('toggles proposal selection and tracks the count', async () => {
    await setup();
    expect(component.isProposalSelected('j1', 'p0')).toBe(false);
    component.toggleProposal('j1', 'p0');
    expect(component.isProposalSelected('j1', 'p0')).toBe(true);
    expect(component.selectedCount('j1')).toBe(1);
    component.toggleProposal('j1', 'p0');
    expect(component.isProposalSelected('j1', 'p0')).toBe(false);
    expect(component.selectedCount('j1')).toBe(0);
  });

  it('files selected proposals and merges the updated list back', async () => {
    await setup();
    const rec = terminalRecordWith([proposal('p0'), proposal('p1')]);
    integrationsSpy.createGitHubReviewIssues.mockReturnValue(
      of({
        job_id: 'j1',
        created: [
          { proposal_id: 'p0', issue_number: 5, issue_url: 'https://x/issues/5', title: 't' },
        ],
        proposals: [
          proposal('p0', { issue_number: 5, issue_url: 'https://x/issues/5' }),
          proposal('p1'),
        ],
      }),
    );
    component.toggleProposal('j1', 'p0');
    component.createIssuesFor(rec);
    expect(integrationsSpy.createGitHubReviewIssues).toHaveBeenCalledWith(
      'acme',
      'widgets',
      'j1',
      ['p0'],
    );
    // The record's proposals now reflect the filed issue; selection is cleared.
    expect(rec.reviewSummary?.pending_issue_proposals?.[0].issue_url).toBe('https://x/issues/5');
    expect(component.selectedCount('j1')).toBe(0);
    expect(component.openProposals(rec).length).toBe(1);
    expect(component.isCreatingIssues('j1')).toBe(false);
  });

  it('does nothing when creating issues with no selection', async () => {
    await setup();
    const rec = terminalRecordWith([proposal('p0')]);
    component.createIssuesFor(rec);
    expect(integrationsSpy.createGitHubReviewIssues).not.toHaveBeenCalled();
  });

  it('drops a proposal skipped server-side from the selection, not just the ones it created', async () => {
    await setup();
    const rec = terminalRecordWith([proposal('p0'), proposal('p1')]);
    // p1 was already filed by another tab before this request landed: the
    // server skips it (not in `created`) but the returned proposals already
    // show its issue_url set.
    integrationsSpy.createGitHubReviewIssues.mockReturnValue(
      of({
        job_id: 'j1',
        created: [
          { proposal_id: 'p0', issue_number: 5, issue_url: 'https://x/issues/5', title: 't' },
        ],
        proposals: [
          proposal('p0', { issue_number: 5, issue_url: 'https://x/issues/5' }),
          proposal('p1', { issue_number: 9, issue_url: 'https://x/issues/9' }),
        ],
      }),
    );
    component.toggleProposal('j1', 'p0');
    component.toggleProposal('j1', 'p1');
    component.createIssuesFor(rec);
    // Both are gone from the selection — p0 because it was just created, p1
    // because the updated proposal list shows it is no longer open — instead
    // of p1 lingering selected forever because it was never in `created`.
    expect(component.selectedCount('j1')).toBe(0);
  });

  it('surfaces a create-issue error', async () => {
    await setup();
    const rec = terminalRecordWith([proposal('p0')]);
    integrationsSpy.createGitHubReviewIssues.mockReturnValue(
      throwError(() => ({ error: { detail: 'no scope' } })),
    );
    component.toggleProposal('j1', 'p0');
    component.createIssuesFor(rec);
    expect(component.createIssueErrorFor('j1')).toBe('no scope');
    expect(component.isCreatingIssues('j1')).toBe(false);
  });

  it('renders the proposals section and creates issues from the template', async () => {
    integrationsSpy.getGitHubReviewHistory.mockReturnValue(
      of([
        {
          job_id: 'j9',
          pr_number: 1,
          pr_url: 'https://example.com/pull/1',
          status: 'completed',
          review_summary: {
            total_issues: 0,
            inline_comments: 0,
            event: 'COMMENT',
            pending_issue_proposals: [proposal('p0', { description: 'latent leak' })],
          },
          created_at: '2026-01-01T00:00:00Z',
        } as CodeReviewRunItem,
      ]),
    );
    integrationsSpy.createGitHubReviewIssues.mockReturnValue(
      of({
        job_id: 'j9',
        created: [
          { proposal_id: 'p0', issue_number: 1, issue_url: 'https://x/1', title: 't' },
        ],
        proposals: [proposal('p0', { issue_number: 1, issue_url: 'https://x/1' })],
      }),
    );
    await setup();
    component.togglePull(component.pulls[0]);
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('.cr-proposals')?.textContent).toContain('latent leak');
    // Select the proposal checkbox, then click "Create GitHub issue(s)".
    const checkbox = host.querySelector('.cr-proposal__select input') as HTMLInputElement;
    checkbox.click();
    fixture.detectChanges();
    const button = Array.from(host.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Create GitHub issue'),
    ) as HTMLButtonElement;
    button.click();
    fixture.detectChanges();
    expect(integrationsSpy.createGitHubReviewIssues).toHaveBeenCalledWith(
      'acme',
      'widgets',
      'j9',
      ['p0'],
    );
    expect(host.querySelector('.cr-proposal__filed')).toBeTruthy();
  });

  it('treats a matched-existing proposal as not open/selectable, same as a filed one', async () => {
    await setup();
    const rec = terminalRecordWith([
      proposal('p0', {
        issue_number: 42,
        issue_url: 'https://x/issues/42',
        matched_existing: true,
      }),
    ]);
    expect(component.openProposals(rec).length).toBe(0);
  });

  it('renders "already tracked" for a matched proposal and "filed" for a Khala-filed one', async () => {
    integrationsSpy.getGitHubReviewHistory.mockReturnValue(
      of([
        {
          job_id: 'j9',
          pr_number: 1,
          pr_url: 'https://example.com/pull/1',
          status: 'completed',
          review_summary: {
            total_issues: 0,
            inline_comments: 0,
            event: 'COMMENT',
            pending_issue_proposals: [
              proposal('p0', {
                description: 'already tracked bug',
                issue_number: 42,
                issue_url: 'https://x/issues/42',
                matched_existing: true,
              }),
              proposal('p1', {
                description: 'freshly filed bug',
                issue_number: 7,
                issue_url: 'https://x/issues/7',
              }),
            ],
          },
          created_at: '2026-01-01T00:00:00Z',
        } as CodeReviewRunItem,
      ]),
    );
    await setup();
    component.togglePull(component.pulls[0]);
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('.cr-proposal__matched')?.textContent).toContain('already tracked');
    expect(host.querySelector('.cr-proposal__filed')?.textContent).toContain('filed');
  });

});
