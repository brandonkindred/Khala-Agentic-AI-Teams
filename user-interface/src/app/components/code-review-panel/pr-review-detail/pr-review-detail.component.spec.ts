import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { PrReviewDetailComponent } from './pr-review-detail.component';
import type { GitHubPullRequestItem } from '../../../models/integrations.model';
import type { PendingIssueProposal } from '../../../models/coding-team.model';
import type { PrReviewRecord } from '../pr-review-record.model';

function makePull(over: Partial<GitHubPullRequestItem> = {}): GitHubPullRequestItem {
  return {
    number: 1,
    title: 'Add widget',
    body_preview: 'Adds a widget to the factory.',
    author: 'octocat',
    html_url: 'https://example.com/pull/1',
    head: 'feature-1',
    base: 'main',
    draft: true,
    labels: ['needs-review'],
    updated_at: '2026-01-01T00:00:00Z',
    ...over,
  };
}

function record(over: Partial<PrReviewRecord> = {}): PrReviewRecord {
  return {
    jobId: 'j1',
    prNumber: 1,
    owner: 'acme',
    repo: 'widgets',
    startedAt: Date.parse('2026-01-01T09:30:00Z'),
    status: 'running',
    ...over,
  };
}

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

describe('PrReviewDetailComponent', () => {
  let component: PrReviewDetailComponent;
  let fixture: ComponentFixture<PrReviewDetailComponent>;

  /** Create the component with sensible defaults, applying any per-test input overrides. */
  async function setup(inputs: Partial<PrReviewDetailComponent> = {}): Promise<void> {
    await TestBed.configureTestingModule({
      imports: [PrReviewDetailComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(PrReviewDetailComponent);
    component = fixture.componentInstance;
    // Defaults; individual tests override via `inputs`.
    component.pull = makePull();
    component.reviews = [];
    component.starting = false;
    component.reviewError = null;
    component.creatingIssues = new Set<string>();
    component.createIssueErrors = new Map<string, string>();
    Object.assign(component, inputs);
    fixture.detectChanges();
  }

  afterEach(() => {
    fixture?.destroy();
  });

  function el(): HTMLElement {
    return fixture.nativeElement as HTMLElement;
  }

  // -------------------------------------------------------------------------
  // PR header
  // -------------------------------------------------------------------------

  it('renders the PR header, meta, preview, and hint', async () => {
    await setup({ pull: makePull({ number: 7, title: 'Fix bug', author: 'dev', body_preview: 'a preview' }) });
    const host = el();
    expect(host.querySelector('.cr-pr-number')?.textContent).toContain('#7');
    expect(host.querySelector('.cr-pr-title')?.textContent).toContain('Fix bug');
    expect(host.querySelector('.cr-chip--draft')).toBeTruthy();
    const meta = host.querySelector('.cr-pull-detail__meta')?.textContent ?? '';
    expect(meta).toContain('feature-1');
    expect(meta).toContain('main');
    expect(meta).toContain('by dev');
    expect(host.querySelector('.cr-pull-detail__preview')?.textContent).toContain('a preview');
    expect(host.querySelector('.cr-pull-detail__hint')).toBeTruthy();
  });

  it('omits the draft chip, author, and preview when they are absent', async () => {
    await setup({ pull: makePull({ draft: false, author: '', body_preview: '' }) });
    const host = el();
    expect(host.querySelector('.cr-chip--draft')).toBeNull();
    expect(host.querySelector('.cr-pull-detail__preview')).toBeNull();
    expect(host.querySelector('.cr-pull-detail__meta')?.textContent).not.toContain('by');
  });

  // -------------------------------------------------------------------------
  // Start Review action
  // -------------------------------------------------------------------------

  it('enables Start Review and emits the pull on click', async () => {
    const pull = makePull({ number: 4 });
    await setup({ pull });
    const emitted: GitHubPullRequestItem[] = [];
    component.startReviewRequested.subscribe((p) => emitted.push(p));
    const button = el().querySelector('.cr-pull-detail__actions button') as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(button.textContent).toContain('Start Review');
    button.click();
    expect(emitted).toEqual([pull]);
  });

  it('disables Start Review and shows a spinner while starting', async () => {
    await setup({ starting: true });
    const button = el().querySelector('.cr-pull-detail__actions button') as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.textContent).toContain('Starting');
    expect(button.querySelector('mat-spinner')).toBeTruthy();
  });

  // -------------------------------------------------------------------------
  // Start-review error banner
  // -------------------------------------------------------------------------

  it('renders the per-PR review error banner when set', async () => {
    await setup({ reviewError: 'no such PR' });
    const banner = el().querySelector('.cr-pull-detail > app-inline-banner[variant="error"]');
    expect(banner).toBeTruthy();
    expect(banner?.textContent).toContain('no such PR');
  });

  it('renders no error banner when reviewError is null and there are no records', async () => {
    await setup();
    expect(el().querySelector('app-inline-banner')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Reviews table
  // -------------------------------------------------------------------------

  it('renders no reviews table when there are no records', async () => {
    await setup({ reviews: [] });
    expect(el().querySelector('.cr-reviews-table')).toBeNull();
  });

  it('renders one row per review run with status, outcome, findings, started, and link', async () => {
    const completed = record({
      jobId: 'done',
      status: 'completed',
      prUrl: 'https://example.com/pull/1',
      reviewSummary: {
        total_issues: 3,
        inline_comments: 2,
        comment_findings: 1,
        event: 'REQUEST_CHANGES',
      },
    });
    const running = record({ jobId: 'live', status: 'running' });
    await setup({ reviews: [completed, running] });
    const rows = el().querySelectorAll('.cr-reviews-table tbody tr');
    expect(rows.length).toBe(2);

    // Completed run: outcome chip, findings chips, working link, no spinner.
    const done = rows[0];
    expect(done.querySelector('.cr-job-status')?.textContent).toContain('completed');
    expect(done.querySelector('mat-spinner')).toBeNull();
    expect(done.querySelector('.cr-chip--event')?.textContent).toContain('REQUEST_CHANGES');
    const findings = done.querySelector('.cr-findings')?.textContent ?? '';
    expect(findings).toContain('3 finding(s)');
    expect(findings).toContain('2 inline');
    expect(findings).toContain('1 comments');
    const link = done.querySelector('a[aria-label="Open PR"]') as HTMLAnchorElement;
    expect(link?.getAttribute('href')).toBe('https://example.com/pull/1');

    // Running run: spinner shown, outcome/findings/link fall back to a dash.
    const live = rows[1];
    expect(live.querySelector('.cr-job-status')?.textContent).toContain('running');
    expect(live.querySelector('mat-spinner')).toBeTruthy();
    expect(live.querySelector('.cr-chip--event')).toBeNull();
    expect(live.querySelector('.cr-findings')).toBeNull();
    expect(live.querySelector('a[aria-label="Open PR"]')).toBeNull();
  });

  it('shows a run error in the outcome cell without a spinner', async () => {
    const errored = record({ status: 'running', error: 'Lost connection' });
    await setup({ reviews: [errored] });
    const row = el().querySelector('.cr-reviews-table tbody tr')!;
    expect(row.querySelector('.cr-job-status--failed')?.textContent).toContain('Lost connection');
    // An errored (even non-terminal) run does not show the in-progress spinner.
    expect(row.querySelector('mat-spinner')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Pending-issue proposals delegation
  // -------------------------------------------------------------------------

  it('renders the proposals child for a terminal run with proposals', async () => {
    await setup({ reviews: [terminalRecordWith([proposal('p0', { description: 'latent leak' })])] });
    const host = el();
    expect(host.querySelector('app-pending-issue-proposals')).toBeTruthy();
    expect(host.querySelector('.cr-proposals')?.textContent).toContain('latent leak');
  });

  it('does not render proposals for non-terminal runs or runs without proposals', async () => {
    const running = record({
      jobId: 'run',
      status: 'running',
      reviewSummary: { total_issues: 0, inline_comments: 0, event: 'COMMENT', pending_issue_proposals: [proposal('p0')] },
    });
    const terminalNoProposals = { ...terminalRecordWith([]), jobId: 'term' };
    await setup({ reviews: [running, terminalNoProposals] });
    expect(el().querySelector('app-pending-issue-proposals')).toBeNull();
  });

  it('passes the per-job creating flag and error down to the proposals child', async () => {
    const rec = terminalRecordWith([proposal('p0')]);
    await setup({
      reviews: [rec],
      creatingIssues: new Set<string>(['j1']),
      createIssueErrors: new Map<string, string>([['j1', 'no scope']]),
    });
    const proposals = el().querySelector('.cr-proposals')!;
    expect(proposals.querySelector('button')?.textContent).toContain('Creating');
    expect(proposals.querySelector('app-inline-banner')?.textContent).toContain('no scope');
  });

  it('re-emits createIssuesRequested with the record and selected ids', async () => {
    const rec = terminalRecordWith([proposal('p0')]);
    await setup({ reviews: [rec] });
    const emitted: { record: PrReviewRecord; ids: string[] }[] = [];
    component.createIssuesRequested.subscribe((e) => emitted.push(e));

    // Select the proposal, then click the "Create GitHub issue(s)" button in the child.
    const host = el();
    (host.querySelector('.cr-proposal__select input') as HTMLInputElement).click();
    fixture.detectChanges();
    const button = Array.from(host.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Create GitHub issue'),
    ) as HTMLButtonElement;
    button.click();

    expect(emitted).toEqual([{ record: rec, ids: ['p0'] }]);
  });

  // -------------------------------------------------------------------------
  // View helpers
  // -------------------------------------------------------------------------

  it('reports per-record terminality', async () => {
    await setup();
    expect(component.isRecordTerminal(record({ status: 'running' }))).toBe(false);
    expect(component.isRecordTerminal(record({ status: 'completed' }))).toBe(true);
  });

  it('normalizes the standalone-comment count across the body_findings rename', async () => {
    await setup();
    expect(
      component.commentFindings({ total_issues: 3, inline_comments: 1, comment_findings: 2, event: 'COMMENT' }),
    ).toBe(2);
    expect(
      component.commentFindings({ total_issues: 3, inline_comments: 1, body_findings: 2, event: 'COMMENT' }),
    ).toBe(2);
    expect(component.commentFindings({ total_issues: 0, inline_comments: 0, event: 'COMMENT' })).toBe(0);
  });

  it('exposes proposals only for terminal runs that have them', async () => {
    await setup();
    const running = record({
      status: 'running',
      reviewSummary: { total_issues: 0, inline_comments: 0, event: 'COMMENT', pending_issue_proposals: [proposal('p0')] },
    });
    expect(component.hasProposals(running)).toBe(false);
    expect(component.hasProposals(terminalRecordWith([proposal('p0')]))).toBe(true);
    expect(component.hasProposals(terminalRecordWith([]))).toBe(false);
  });

  it('reads the create-issue in-flight flag and error from its inputs', async () => {
    await setup({
      creatingIssues: new Set<string>(['j1']),
      createIssueErrors: new Map<string, string>([['j1', 'no scope']]),
    });
    expect(component.isCreatingIssues('j1')).toBe(true);
    expect(component.isCreatingIssues('other')).toBe(false);
    expect(component.createIssueErrorFor('j1')).toBe('no scope');
    expect(component.createIssueErrorFor('other')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Live update via in-place record mutation
  // -------------------------------------------------------------------------

  it('reflects an in-place record mutation on the next change detection', async () => {
    const rec = record({ status: 'running' });
    await setup({ reviews: [rec] });
    let cell = el().querySelector('.cr-reviews-table tbody tr td .cr-job-status');
    expect(cell?.textContent).toContain('running');
    expect(el().querySelector('.cr-reviews-table tbody tr mat-spinner')).toBeTruthy();

    // The parent's poller mutates the same object reference, then triggers CD.
    rec.status = 'completed';
    rec.reviewSummary = { total_issues: 1, inline_comments: 0, comment_findings: 0, event: 'REQUEST_CHANGES' };
    fixture.detectChanges();

    cell = el().querySelector('.cr-reviews-table tbody tr td .cr-job-status');
    expect(cell?.textContent).toContain('completed');
    expect(el().querySelector('.cr-chip--event')?.textContent).toContain('REQUEST_CHANGES');
    expect(el().querySelector('.cr-reviews-table tbody tr mat-spinner')).toBeNull();
  });
});
