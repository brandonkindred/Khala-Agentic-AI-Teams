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
import type {
  CodeReviewRunItem,
  GitHubConfigResponse,
  GitHubPullRequestItem,
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
    startedAt: Date.parse('2026-01-01T00:00:00Z'),
    status: 'running',
    ...over,
  };
}

const CONFIGURED: GitHubConfigResponse = {
  enabled: true,
  token_configured: true,
  owner: 'acme',
  repo: 'widgets',
  default_label: 'ai',
};

const UNCONFIGURED: GitHubConfigResponse = {
  enabled: false,
  token_configured: false,
  owner: '',
  repo: '',
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
    getGitHubPullRequests: ReturnType<typeof vi.fn>;
    runGitHubReviewPr: ReturnType<typeof vi.fn>;
    getGitHubReviewHistory: ReturnType<typeof vi.fn>;
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
  }

  beforeEach(() => {
    vi.useFakeTimers();
    apiSpy = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      getJobStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running' })),
    };
    integrationsSpy = {
      getGitHubConfig: vi.fn().mockReturnValue(of(CONFIGURED)),
      getGitHubPullRequests: vi.fn().mockReturnValue(of(makePulls(3))),
      runGitHubReviewPr: vi.fn().mockReturnValue(
        of({ job_id: 'j1', pr_number: 1, pr_url: 'https://example.com/pull/1', status: 'pending', message: '' }),
      ),
      getGitHubReviewHistory: vi.fn().mockReturnValue(of([])),
    };
  });

  afterEach(() => {
    fixture?.destroy();
    vi.useRealTimers();
  });

  // -------------------------------------------------------------------------
  // Config + list loading
  // -------------------------------------------------------------------------

  it('should create and load pull requests + review history when configured', async () => {
    await setup();
    expect(component.githubConfigured).toBe(true);
    expect(integrationsSpy.getGitHubPullRequests).toHaveBeenCalled();
    expect(integrationsSpy.getGitHubReviewHistory).toHaveBeenCalled();
    expect(component.pulls.length).toBe(3);
    expect(component.pullsLoaded).toBe(true);
  });

  it('does not load pull requests when GitHub is unconfigured', async () => {
    integrationsSpy.getGitHubConfig.mockReturnValue(of(UNCONFIGURED));
    await setup();
    expect(component.githubConfigured).toBe(false);
    expect(integrationsSpy.getGitHubPullRequests).not.toHaveBeenCalled();
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

  it('paginates the PR list client-side', async () => {
    integrationsSpy.getGitHubPullRequests.mockReturnValue(of(makePulls(25)));
    await setup();
    expect(component.pagedPulls.length).toBe(10);
    component.onPageChange({ pageIndex: 1, pageSize: 10, length: 25 });
    expect(component.pageIndex).toBe(1);
    expect(component.pagedPulls[0].number).toBe(11);
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
    expect(integrationsSpy.runGitHubReviewPr).toHaveBeenCalledWith({ pr_number: 1 });
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
    component.starting.add(1);
    component.startReview(component.pulls[0]);
    expect(integrationsSpy.runGitHubReviewPr).not.toHaveBeenCalled();
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
    expect(component.starting.has(1)).toBe(false);
  });

  it('falls back to err.message when a start-review error has no detail', async () => {
    integrationsSpy.runGitHubReviewPr.mockReturnValue(throwError(() => ({ message: 'Network down' })));
    await setup();
    component.startReview(component.pulls[0]);
    expect(component.reviewErrorFor(1)).toBe('Network down');
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
});
