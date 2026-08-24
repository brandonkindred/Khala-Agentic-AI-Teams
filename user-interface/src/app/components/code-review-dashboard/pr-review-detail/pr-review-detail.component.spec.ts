import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialog } from '@angular/material/dialog';
import { PrReviewDetailComponent } from './pr-review-detail.component';
import { CodeReviewTranscriptDialogComponent } from '../code-review-transcript-dialog/code-review-transcript-dialog.component';
import { CodeReviewSystemicFindingsDialogComponent } from '../code-review-systemic-findings-dialog/code-review-systemic-findings-dialog.component';
import type { GitHubPullRequestItem } from '../../../models/integrations.model';
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

describe('PrReviewDetailComponent', () => {
  let component: PrReviewDetailComponent;
  let fixture: ComponentFixture<PrReviewDetailComponent>;
  let dialogOpen: ReturnType<typeof vi.fn>;

  /** Create the component with sensible defaults, applying any per-test input overrides. */
  async function setup(inputs: Partial<PrReviewDetailComponent> = {}): Promise<void> {
    dialogOpen = vi.fn();
    await TestBed.configureTestingModule({
      imports: [PrReviewDetailComponent, NoopAnimationsModule],
      providers: [{ provide: MatDialog, useValue: { open: dialogOpen } }],
    }).compileComponents();

    fixture = TestBed.createComponent(PrReviewDetailComponent);
    component = fixture.componentInstance;
    // Defaults; individual tests override via `inputs`.
    component.pull = makePull();
    component.reviews = [];
    component.starting = false;
    component.reviewError = null;
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

  it('renders one row per review run with status, outcome, findings, severity, started, and duration', async () => {
    const completed = record({
      jobId: 'done',
      status: 'completed',
      prUrl: 'https://example.com/pull/1',
      completedAt: Date.parse('2026-01-01T09:32:00Z'), // 2m after startedAt (09:30)
      reviewSummary: {
        total_issues: 3,
        inline_comments: 2,
        comment_findings: 1,
        event: 'REQUEST_CHANGES',
        severity_counts: { critical: 1, high: 0, medium: 2 },
      },
    });
    const running = record({ jobId: 'live', status: 'running' });
    await setup({ reviews: [completed, running] });
    const rows = el().querySelectorAll('.cr-reviews-table tbody tr');
    expect(rows.length).toBe(2);

    // Completed run: outcome chip, findings chips, non-zero severity chips, duration, no spinner.
    const done = rows[0];
    expect(done.querySelector('.cr-job-status')?.textContent).toContain('completed');
    expect(done.querySelector('mat-spinner')).toBeNull();
    expect(done.querySelector('.cr-chip--event')?.textContent).toContain('REQUEST_CHANGES');
    const findings = done.querySelector('.cr-findings')?.textContent ?? '';
    expect(findings).toContain('3 finding(s)');
    expect(findings).toContain('2 inline');
    expect(findings).toContain('1 comments');
    // Only non-zero severity levels render, in critical→info order.
    const sevChips = Array.from(done.querySelectorAll('[class*="cr-chip--sev-"]'));
    expect(sevChips.map((c) => c.textContent?.trim())).toEqual(['1 critical', '2 medium']);
    expect(done.querySelector('.cr-chip--sev-high')).toBeNull();
    // Columns: Status | Outcome | Findings | Severity | Started | Duration | Transcript.
    const doneCells = done.querySelectorAll('td');
    expect(doneCells[doneCells.length - 2].textContent?.trim()).toBe('2m 0s'); // duration
    // The per-run link column is gone; the PR link is hoisted to the header.
    expect(done.querySelector('a[aria-label="Open PR"]')).toBeNull();

    // Running run: spinner shown, outcome/findings/severity/duration fall back to a dash.
    const live = rows[1];
    expect(live.querySelector('.cr-job-status')?.textContent).toContain('running');
    expect(live.querySelector('mat-spinner')).toBeTruthy();
    expect(live.querySelector('.cr-chip--event')).toBeNull();
    expect(live.querySelector('.cr-findings')).toBeNull();
    expect(live.querySelector('[class*="cr-chip--sev-"]')).toBeNull();
    const liveCells = live.querySelectorAll('td');
    expect(liveCells[liveCells.length - 2].textContent?.trim()).toBe('—'); // duration
    expect(liveCells[liveCells.length - 1].textContent?.trim()).toBe('—'); // transcript (non-terminal)
  });

  it('hoists a single PR link into the detail header and drops the per-row link', async () => {
    await setup({
      pull: makePull({ html_url: 'https://example.com/pull/9' }),
      reviews: [record({ jobId: 'r1', status: 'completed', prUrl: 'https://example.com/pull/9' })],
    });
    const host = el();
    // The PR link lives once in the detail header, sourced from the PR's html_url.
    const link = host.querySelector('.cr-pull-detail__link') as HTMLAnchorElement | null;
    expect(link).toBeTruthy();
    expect(link?.getAttribute('href')).toBe('https://example.com/pull/9');
    // No per-run link remains inside the reviews table.
    expect(host.querySelector('.cr-reviews-table tbody a')).toBeNull();
  });

  it('shows an em dash for severity when a run has no severity counts', async () => {
    await setup({
      reviews: [
        record({
          jobId: 'r1',
          status: 'completed',
          reviewSummary: { total_issues: 0, inline_comments: 0, event: 'APPROVE' },
        }),
      ],
    });
    const cells = el().querySelector('.cr-reviews-table tbody tr')!.querySelectorAll('td');
    // Columns: Status | Outcome | Findings | Severity | Started | Duration.
    expect(cells[3].textContent?.trim()).toBe('—'); // severity
    expect(el().querySelector('[class*="cr-chip--sev-"]')).toBeNull();
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
  // View Transcript action
  // -------------------------------------------------------------------------

  it('shows a View Transcript button only for terminal runs', async () => {
    const completed = record({ jobId: 'done', status: 'completed' });
    const running = record({ jobId: 'live', status: 'running' });
    await setup({ reviews: [completed, running] });
    const rows = el().querySelectorAll('.cr-reviews-table tbody tr');
    expect(rows[0].querySelector('button[aria-label="View review transcript"]')).toBeTruthy();
    expect(rows[1].querySelector('button[aria-label="View review transcript"]')).toBeNull();
  });

  it('opens the transcript dialog with the record owner/repo/jobId on click', async () => {
    const rec = record({ jobId: 'j9', owner: 'acme', repo: 'widgets', status: 'completed' });
    await setup({ reviews: [rec] });
    const button = el().querySelector(
      '.cr-reviews-table tbody tr button[aria-label="View review transcript"]',
    ) as HTMLButtonElement;
    button.click();
    expect(dialogOpen).toHaveBeenCalledWith(
      CodeReviewTranscriptDialogComponent,
      expect.objectContaining({ data: { owner: 'acme', repo: 'widgets', jobId: 'j9' } }),
    );
  });

  // -------------------------------------------------------------------------
  // View Systemic Findings action
  // -------------------------------------------------------------------------

  it('shows the systemic-findings chip only when the run has systemic findings', async () => {
    const withFindings = record({
      jobId: 'a',
      status: 'completed',
      reviewSummary: {
        total_issues: 2,
        inline_comments: 2,
        event: 'REQUEST_CHANGES',
        systemic_findings: [
          { title: 't', description: 'd', related_locations: [] },
          { title: 't2', description: 'd2', related_locations: [] },
        ],
      },
    });
    const withoutFindings = record({
      jobId: 'b',
      status: 'completed',
      reviewSummary: { total_issues: 1, inline_comments: 1, event: 'COMMENT', systemic_findings: [] },
    });
    await setup({ reviews: [withFindings, withoutFindings] });
    const rows = el().querySelectorAll('.cr-reviews-table tbody tr');
    const chip = rows[0].querySelector('.cr-chip--systemic');
    expect(chip).toBeTruthy();
    expect(chip?.textContent).toContain('2 systemic pattern(s)');
    expect(chip?.getAttribute('aria-label')).toBe('View systemic findings');
    expect(rows[1].querySelector('.cr-chip--systemic')).toBeNull();
  });

  it('opens the systemic-findings dialog with the record findings on click', async () => {
    const findings = [{ title: 't', description: 'd', related_locations: [] }];
    const rec = record({
      jobId: 'j9',
      status: 'completed',
      reviewSummary: { total_issues: 1, inline_comments: 1, event: 'COMMENT', systemic_findings: findings },
    });
    await setup({ reviews: [rec] });
    const chip = el().querySelector('.cr-chip--systemic') as HTMLButtonElement;
    chip.click();
    expect(dialogOpen).toHaveBeenCalledWith(
      CodeReviewSystemicFindingsDialogComponent,
      expect.objectContaining({ data: { findings } }),
    );
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

  it('counts systemic findings, treating absent/empty as 0', async () => {
    await setup();
    expect(
      component.systemicFindingsCount({
        total_issues: 2,
        inline_comments: 2,
        event: 'COMMENT',
        systemic_findings: [
          { title: 't', description: 'd', related_locations: [] },
          { title: 't2', description: 'd2', related_locations: [] },
        ],
      }),
    ).toBe(2);
    expect(
      component.systemicFindingsCount({ total_issues: 0, inline_comments: 0, event: 'COMMENT' }),
    ).toBe(0);
    expect(
      component.systemicFindingsCount({
        total_issues: 0,
        inline_comments: 0,
        event: 'COMMENT',
        systemic_findings: [],
      }),
    ).toBe(0);
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
