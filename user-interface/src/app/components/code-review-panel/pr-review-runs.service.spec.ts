import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, Subject, throwError } from 'rxjs';
import { vi } from 'vitest';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { PrReviewRunsService } from './pr-review-runs.service';
import type { PrReviewRecord } from './pr-review-record.model';
import type { PendingIssueProposal } from '../../models/coding-team.model';
import type { CodeReviewRunItem, GitHubPullRequestItem, GitHubRepoItem } from '../../models/integrations.model';

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

/**
 * `PrReviewRunsService` is provided at `CodeReviewPanelComponent`'s own component level in
 * production (so `inject(ChangeDetectorRef)` inside it resolves to that host's change
 * detector) — this minimal host reproduces that same component-provider context for tests.
 */
@Component({ standalone: true, template: '', providers: [PrReviewRunsService] })
class TestHostComponent {}

describe('PrReviewRunsService', () => {
  let service: PrReviewRunsService;
  let fixture: ComponentFixture<TestHostComponent>;
  let apiSpy: { getJobStatus: ReturnType<typeof vi.fn> };
  let integrationsSpy: {
    runGitHubReviewPr: ReturnType<typeof vi.fn>;
    getGitHubReviewHistory: ReturnType<typeof vi.fn>;
    createGitHubReviewIssues: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    vi.useFakeTimers();
    apiSpy = {
      getJobStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running' })),
    };
    integrationsSpy = {
      runGitHubReviewPr: vi.fn().mockReturnValue(
        of({ job_id: 'j1', pr_number: 1, pr_url: 'https://example.com/pull/1', status: 'pending', message: '' }),
      ),
      getGitHubReviewHistory: vi.fn().mockReturnValue(of([])),
      createGitHubReviewIssues: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [
        { provide: CodingTeamApiService, useValue: apiSpy },
        { provide: IntegrationsApiService, useValue: integrationsSpy },
      ],
    });
    fixture = TestBed.createComponent(TestHostComponent);
    service = fixture.debugElement.injector.get(PrReviewRunsService);
    fixture.detectChanges();
  });

  afterEach(() => {
    fixture?.destroy();
    vi.useRealTimers();
  });

  // -------------------------------------------------------------------------
  // Hydration from the backend
  // -------------------------------------------------------------------------

  it('hydrates the reviews map from history and resumes pollers for non-terminal runs', () => {
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
    service.reset(REPO);
    service.hydrate(REPO);

    expect(service.reviewsFor(1).map((r) => r.jobId)).toEqual(['done-1', 'live-1']);
    expect(service.reviewsFor(2).map((r) => r.jobId)).toEqual(['done-2']);
    // The running run keeps polling; the two completed runs do not.
    expect(service['pollers'].has('live-1')).toBe(true);
    expect(service['pollers'].has('done-1')).toBe(false);
    // The invalid created_at fell back to a real timestamp.
    expect(Number.isNaN(service.reviewsFor(1)[1].startedAt)).toBe(false);
  });

  it('survives a review-history load failure', () => {
    integrationsSpy.getGitHubReviewHistory.mockReturnValue(throwError(() => new Error('nope')));
    service.reset(REPO);
    service.hydrate(REPO);
    expect(service.reviews.size).toBe(0);
  });

  it('keeps an in-flight review that a concurrent hydrate snapshot omits', () => {
    service.reset(REPO);
    // A hydrate is in flight (response deferred) when the user starts a review.
    const hydrate$ = new Subject<CodeReviewRunItem[]>();
    integrationsSpy.getGitHubReviewHistory.mockReturnValue(hydrate$.asObservable());
    apiSpy.getJobStatus.mockReturnValue(of({ job_id: 'A', status: 'running' }));
    integrationsSpy.runGitHubReviewPr.mockReturnValue(
      of({ job_id: 'A', pr_number: 1, pr_url: 'u', status: 'pending', message: '' }),
    );

    service.hydrate(REPO); // hydrate request fired, not yet resolved
    service.startReview(REPO, makePulls(1)[0]); // record A + live poller A
    expect(service.reviewsFor(1).map((r) => r.jobId)).toContain('A');
    expect(service['pollers'].has('A')).toBe(true);

    // The snapshot (taken before A was persisted) arrives without A.
    hydrate$.next([]);
    expect(service.reviewsFor(1).map((r) => r.jobId)).toContain('A');
    expect(service['pollers'].has('A')).toBe(true);
  });

  it('prefers the live in-flight record over a stale hydrate snapshot copy', () => {
    service.reset(REPO);
    const hydrate$ = new Subject<CodeReviewRunItem[]>();
    integrationsSpy.getGitHubReviewHistory.mockReturnValue(hydrate$.asObservable());
    apiSpy.getJobStatus.mockReturnValue(of({ job_id: 'A', status: 'running', status_text: 'live' }));
    integrationsSpy.runGitHubReviewPr.mockReturnValue(
      of({ job_id: 'A', pr_number: 1, pr_url: 'u', status: 'pending', message: '' }),
    );

    service.hydrate(REPO);
    service.startReview(REPO, makePulls(1)[0]);
    vi.advanceTimersByTime(5000); // poll advances A to running + status_text 'live'
    const liveRec = service.reviewsFor(1).find((r) => r.jobId === 'A')!;

    // The snapshot includes A but with stale 'pending' status.
    hydrate$.next([{ job_id: 'A', pr_number: 1, status: 'pending', created_at: '2026-01-01T00:00:00Z' }]);

    const afterRec = service.reviewsFor(1).find((r) => r.jobId === 'A')!;
    expect(afterRec).toBe(liveRec); // same object, not a fresh snapshot copy
    expect(afterRec.statusText).toBe('live'); // live state retained
    expect(service['pollers'].has('A')).toBe(true);
  });

  // -------------------------------------------------------------------------
  // Starting reviews + polling
  // -------------------------------------------------------------------------

  it('starts a review, records it, and polls it live', () => {
    service.reset(REPO);
    apiSpy.getJobStatus.mockReturnValue(
      of({
        job_id: 'j1',
        status: 'completed',
        status_text: 'done',
        github_pr_url: 'https://example.com/pull/1',
        review_summary: { total_issues: 2, inline_comments: 1, comment_findings: 1, event: 'REQUEST_CHANGES' },
      }),
    );
    service.startReview(REPO, makePulls(1)[0]);
    expect(integrationsSpy.runGitHubReviewPr).toHaveBeenCalledWith({ pr_number: 1, owner: 'acme', repo: 'widgets' });
    expect(service.reviewsFor(1).length).toBe(1);
    expect(service.reviewsFor(1)[0].jobId).toBe('j1');

    vi.advanceTimersByTime(5000);
    const rec = service.reviewsFor(1)[0];
    expect(rec.status).toBe('completed');
    expect(rec.reviewSummary?.event).toBe('REQUEST_CHANGES');
    expect(rec.prUrl).toBe('https://example.com/pull/1');
    // Terminal status removes the poller.
    expect(service['pollers'].has('j1')).toBe(false);
  });

  // -------------------------------------------------------------------------
  // Review-duration timestamps (server-clock start + terminal completion)
  // -------------------------------------------------------------------------

  it('maps created_at and completed_at from history into the record', () => {
    integrationsSpy.getGitHubReviewHistory.mockReturnValue(
      of([
        {
          job_id: 'c1',
          pr_number: 1,
          status: 'completed',
          review_summary: { total_issues: 0, inline_comments: 0, event: 'APPROVE' },
          created_at: '2026-02-01T00:00:00Z',
          completed_at: '2026-02-01T00:01:30Z',
        },
        {
          job_id: 'c2',
          pr_number: 1,
          status: 'completed',
          created_at: '2026-02-01T00:00:00Z',
          completed_at: 'not-a-real-date', // unparseable -> completedAt undefined
        },
      ] as CodeReviewRunItem[]),
    );
    service.reset(REPO);
    service.hydrate(REPO);
    const [withTime, badTime] = service.reviewsFor(1);
    expect(withTime.completedAt).toBe(Date.parse('2026-02-01T00:01:30Z'));
    expect(badTime.completedAt).toBeUndefined();
  });

  it('uses the server-clock created_at from the start response as the record start time', () => {
    service.reset(REPO);
    integrationsSpy.runGitHubReviewPr.mockReturnValue(
      of({
        job_id: 'j1',
        pr_number: 1,
        pr_url: 'u',
        status: 'pending',
        message: '',
        created_at: '2026-03-01T00:00:00Z',
      }),
    );
    service.startReview(REPO, makePulls(1)[0]);
    expect(service.reviewsFor(1)[0].startedAt).toBe(Date.parse('2026-03-01T00:00:00Z'));
  });

  it('falls back to the browser clock for the start time when created_at is absent', () => {
    service.reset(REPO);
    // The default runGitHubReviewPr mock carries no created_at.
    service.startReview(REPO, makePulls(1)[0]);
    const rec = service.reviewsFor(1)[0];
    expect(Number.isNaN(rec.startedAt)).toBe(false);
    expect(rec.startedAt).toBeGreaterThan(0);
  });

  it('stamps completedAt from the terminal updated_at when a live poll goes terminal', () => {
    service.reset(REPO);
    // A stale-failed job bumps updated_at to the terminal time but leaves
    // last_activity_at frozen; the duration must use the terminal updated_at.
    apiSpy.getJobStatus.mockReturnValue(
      of({
        job_id: 'j1',
        status: 'failed',
        last_activity_at: '2026-03-01T00:02:00Z', // frozen at last activity
        updated_at: '2026-03-01T00:10:00Z', // terminal transition time (wins)
      }),
    );
    service.startReview(REPO, makePulls(1)[0]);
    const rec = service.reviewsFor(1)[0];
    expect(rec.completedAt).toBeUndefined(); // not terminal yet
    vi.advanceTimersByTime(5000); // one poll tick -> terminal
    expect(rec.completedAt).toBe(Date.parse('2026-03-01T00:10:00Z'));
  });

  it('stops polling once a review reaches a terminal status', () => {
    service.reset(REPO);
    apiSpy.getJobStatus.mockReturnValue(
      of({
        job_id: 'j1',
        status: 'completed',
        review_summary: { total_issues: 0, inline_comments: 0, comment_findings: 0, event: 'APPROVE' },
      }),
    );
    service.startReview(REPO, makePulls(1)[0]);
    vi.advanceTimersByTime(5000); // first poll -> terminal -> poller disposed
    expect(service['pollers'].has('j1')).toBe(false);
    // No further polling after the job is terminal (subscription was torn down).
    const callsAfterTerminal = apiSpy.getJobStatus.mock.calls.length;
    vi.advanceTimersByTime(20000);
    expect(apiSpy.getJobStatus.mock.calls.length).toBe(callsAfterTerminal);
  });

  it('ignores a second startReview while one is already starting', () => {
    service.reset(REPO);
    const slow = new Subject<never>();
    integrationsSpy.runGitHubReviewPr.mockReturnValue(slow.asObservable());
    const pull = makePulls(1)[0];
    service.startReview(REPO, pull);
    service.startReview(REPO, pull); // second call ignored while the first is in flight
    expect(integrationsSpy.runGitHubReviewPr).toHaveBeenCalledTimes(1);
    expect(service.isStarting(REPO, pull.number)).toBe(true);
  });

  it('surfaces a start-review error per PR without touching other PRs', () => {
    integrationsSpy.runGitHubReviewPr.mockReturnValue(throwError(() => ({ error: { detail: 'no such PR' } })));
    service.reset(REPO);
    service.startReview(REPO, makePulls(1)[0]);
    expect(service.reviewErrorFor(1)).toBe('no such PR');
    expect(service.reviewErrorFor(2)).toBeNull();
    expect(service.isStarting(REPO, 1)).toBe(false);
  });

  it('an in-flight start on one repo does not block the same-numbered PR in another repo', () => {
    service.reset(REPO);
    const slow = new Subject<never>();
    integrationsSpy.runGitHubReviewPr.mockReturnValue(slow.asObservable());
    service.startReview(REPO, makePulls(1)[0]); // start acme/widgets PR #1 (in flight)
    // Switch to another repo that also has a PR #1.
    const other: GitHubRepoItem = { ...REPO, full_name: 'other/thing', owner: 'other', name: 'thing' };
    service.reset(other);
    // `starting` is keyed by owner/repo#number, so other/thing PR #1 is NOT considered starting.
    expect(service.isStarting(other, 1)).toBe(false);
  });

  it('falls back to err.message when a start-review error has no detail', () => {
    integrationsSpy.runGitHubReviewPr.mockReturnValue(throwError(() => ({ message: 'Network down' })));
    service.reset(REPO);
    service.startReview(REPO, makePulls(1)[0]);
    expect(service.reviewErrorFor(1)).toBe('Network down');
  });

  it('does not surface a start-review failure from a switched-away repo under the new repo', () => {
    service.reset(REPO); // acme/widgets expanded
    const slow = new Subject<never>();
    integrationsSpy.runGitHubReviewPr.mockReturnValue(slow.asObservable());
    service.startReview(REPO, makePulls(1)[0]); // start on acme/widgets PR #1 (request pending)
    // Switch to a different repo that also has a PR #1 before the start resolves.
    const other: GitHubRepoItem = { ...REPO, full_name: 'other/thing', owner: 'other', name: 'thing' };
    service.reset(other);
    // acme/widgets' start now fails — reviewErrors is keyed by bare PR number, so an
    // unguarded set would render this failure under other/thing's PR #1.
    slow.error({ error: { detail: 'clone failed' } });
    expect(service.reviewErrorFor(1)).toBeNull();
  });

  it('does not spin a poller for a review that resolves after a switch-away', () => {
    service.reset(REPO); // acme/widgets expanded
    const slow = new Subject<{ job_id: string; pr_number: number; pr_url: string; status: string; message: string }>();
    integrationsSpy.runGitHubReviewPr.mockReturnValue(slow.asObservable());
    apiSpy.getJobStatus.mockClear();
    service.startReview(REPO, makePulls(1)[0]); // start acme/widgets PR #1 (pending)
    // Switch to another repo before the start resolves.
    const other: GitHubRepoItem = { ...REPO, full_name: 'other/thing', owner: 'other', name: 'thing' };
    service.reset(other);
    // The start now resolves while the user is on another repo — no record is shown and no
    // orphan poller must be attached (startPolling is inside the same-repo guard).
    slow.next({ job_id: 'jX', pr_number: 1, pr_url: 'u', status: 'pending', message: '' });
    slow.complete();
    vi.advanceTimersByTime(20000);
    expect(apiSpy.getJobStatus).not.toHaveBeenCalledWith('jX');
  });

  it('falls back to a default message when a start-review error has no detail or message', () => {
    integrationsSpy.runGitHubReviewPr.mockReturnValue(throwError(() => ({})));
    service.reset(REPO);
    service.startReview(REPO, makePulls(1)[0]);
    expect(service.reviewErrorFor(1)).toBe('Failed to start review.');
  });

  it('accumulates multiple reviews on the same PR, newest-first', () => {
    service.reset(REPO);
    integrationsSpy.runGitHubReviewPr
      .mockReturnValueOnce(of({ job_id: 'j1', pr_number: 1, pr_url: 'u1', status: 'pending', message: '' }))
      .mockReturnValueOnce(of({ job_id: 'j2', pr_number: 1, pr_url: 'u2', status: 'pending', message: '' }));
    const pull = makePulls(1)[0];
    service.startReview(REPO, pull);
    service.startReview(REPO, pull);
    expect(service.reviewsFor(1).map((r) => r.jobId)).toEqual(['j2', 'j1']);
    expect(service.hasReviews(1)).toBe(true);
  });

  it('polls concurrent reviews on different PRs without cross-talk', () => {
    service.reset(REPO);
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
    const pulls = makePulls(2);
    service.startReview(REPO, pulls[0]);
    service.startReview(REPO, pulls[1]);
    vi.advanceTimersByTime(5000);

    expect(apiSpy.getJobStatus).toHaveBeenCalledWith('ja');
    expect(apiSpy.getJobStatus).toHaveBeenCalledWith('jb');
    expect(service.reviewsFor(1)[0].reviewSummary?.event).toBe('APPROVE');
    expect(service.reviewsFor(2)[0].reviewSummary?.event).toBe('COMMENT');
  });

  it('marks the record errored when polling loses the connection', () => {
    service.reset(REPO);
    apiSpy.getJobStatus.mockReturnValue(throwError(() => new Error('down')));
    service.startReview(REPO, makePulls(1)[0]);
    // Three consecutive failed polls trip the connection-lost handler.
    vi.advanceTimersByTime(15001);
    expect(service.reviewsFor(1)[0].error).toContain('Lost connection');
    expect(service['pollers'].has('j1')).toBe(false);
  });

  // -------------------------------------------------------------------------
  // Derived helpers (row badge + status) — the derivation logic itself is
  // covered by review-metrics.spec.ts; these confirm the service wires
  // `latestReview` into it correctly.
  // -------------------------------------------------------------------------

  it('derives the row badge from the latest review run', () => {
    service.reset(REPO);
    expect(service.badgeLabel(1)).toBeNull();
    expect(service.badgeClass(1)).toBe('');

    service['_reviews'].set(1, [record({ status: 'running' })]);
    expect(service.badgeLabel(1)).toBe('running');
    expect(service.isLatestRunning(1)).toBe(true);

    service['_reviews'].set(1, [record({ status: 'failed' })]);
    expect(service.badgeLabel(1)).toBe('failed');
    expect(service.badgeClass(1)).toBe('cr-job-status--failed');
  });

  // -------------------------------------------------------------------------
  // Teardown
  // -------------------------------------------------------------------------

  it('tears down all pollers on destroy', () => {
    service.reset(REPO);
    integrationsSpy.runGitHubReviewPr
      .mockReturnValueOnce(of({ job_id: 'jx', pr_number: 1, pr_url: 'u', status: 'pending', message: '' }))
      .mockReturnValueOnce(of({ job_id: 'jy', pr_number: 2, pr_url: 'u', status: 'pending', message: '' }));
    apiSpy.getJobStatus.mockReturnValue(of({ job_id: 'jx', status: 'running' })); // stays non-terminal
    const pulls = makePulls(2);
    service.startReview(REPO, pulls[0]);
    service.startReview(REPO, pulls[1]);
    expect(service['pollers'].size).toBe(2);

    const callsBefore = apiSpy.getJobStatus.mock.calls.length;
    service.ngOnDestroy();
    expect(service['pollers'].size).toBe(0);
    vi.advanceTimersByTime(15000);
    expect(apiSpy.getJobStatus.mock.calls.length).toBe(callsBefore);
  });

  // -------------------------------------------------------------------------
  // Pre-existing-bug proposals -> GitHub issues
  // -------------------------------------------------------------------------

  it('files the given proposal ids and merges the updated list back', () => {
    const rec = terminalRecordWith([proposal('p0'), proposal('p1')]);
    integrationsSpy.createGitHubReviewIssues.mockReturnValue(
      of({
        job_id: 'j1',
        created: [{ proposal_id: 'p0', issue_number: 5, issue_url: 'https://x/issues/5', title: 't' }],
        proposals: [proposal('p0', { issue_number: 5, issue_url: 'https://x/issues/5' }), proposal('p1')],
      }),
    );
    service.createIssuesFor(rec, ['p0']);
    expect(integrationsSpy.createGitHubReviewIssues).toHaveBeenCalledWith('acme', 'widgets', 'j1', ['p0']);
    // The record's proposals now reflect the filed issue.
    expect(rec.reviewSummary?.pending_issue_proposals?.[0].issue_url).toBe('https://x/issues/5');
    // The in-flight flag (passed down to the child) is cleared on completion.
    expect(service.creatingIssues.has('j1')).toBe(false);
  });

  it('does nothing when creating issues with no ids', () => {
    const rec = terminalRecordWith([proposal('p0')]);
    service.createIssuesFor(rec, []);
    expect(integrationsSpy.createGitHubReviewIssues).not.toHaveBeenCalled();
  });

  it('surfaces a create-issue error', () => {
    const rec = terminalRecordWith([proposal('p0')]);
    integrationsSpy.createGitHubReviewIssues.mockReturnValue(throwError(() => ({ error: { detail: 'no scope' } })));
    service.createIssuesFor(rec, ['p0']);
    expect(service.createIssueErrors.get('j1')).toBe('no scope');
    expect(service.creatingIssues.has('j1')).toBe(false);
  });
});
