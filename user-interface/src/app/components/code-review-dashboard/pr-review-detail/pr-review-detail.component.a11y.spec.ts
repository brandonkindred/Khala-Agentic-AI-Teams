import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { PrReviewDetailComponent } from './pr-review-detail.component';
import type { GitHubPullRequestItem } from '../../../models/integrations.model';
import type { PrReviewRecord } from '../pr-review-record.model';
import { expectNoAxeViolations } from '../../../testing/a11y';

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

describe('PrReviewDetailComponent a11y', () => {
  async function createFixture(
    inputs: Partial<PrReviewDetailComponent> = {},
  ): Promise<ComponentFixture<PrReviewDetailComponent>> {
    await TestBed.configureTestingModule({
      imports: [PrReviewDetailComponent, NoopAnimationsModule],
    }).compileComponents();

    const fixture = TestBed.createComponent(PrReviewDetailComponent);
    const component = fixture.componentInstance;
    component.pull = makePull();
    component.reviews = [];
    component.starting = false;
    component.reviewError = null;
    component.creatingIssues = new Set<string>();
    component.createIssueErrors = new Map<string, string>();
    Object.assign(component, inputs);
    fixture.detectChanges();
    return fixture;
  }

  it('has no axe violations with a completed review row', async () => {
    const completed = record({
      jobId: 'done',
      status: 'completed',
      prUrl: 'https://example.com/pull/1',
      completedAt: Date.parse('2026-01-01T09:32:00Z'),
      reviewSummary: {
        total_issues: 3,
        inline_comments: 2,
        comment_findings: 1,
        event: 'REQUEST_CHANGES',
        severity_counts: { critical: 1, high: 0, medium: 2 },
        systemic_findings: [{ title: 't', description: 'd', related_locations: [] }],
      },
    });
    const fixture = await createFixture({ reviews: [completed] });
    const host: HTMLElement = fixture.nativeElement;
    // Guard: don't pass axe vacuously against an empty DOM.
    expect(host.querySelector('.cr-pull-detail')).toBeTruthy();
    expect(host.querySelectorAll('.cr-reviews-table tbody tr').length).toBe(1);
    // Also guard that the systemic-findings button is actually present, so
    // axe exercises it (a missing aria-label would otherwise go unchecked).
    expect(host.querySelector('.cr-chip--systemic')).toBeTruthy();
    await expectNoAxeViolations(host);
  }, 15000);

  it('has no axe violations with a running review row and a Start Review error banner', async () => {
    const running = record({ jobId: 'live', status: 'running' });
    const fixture = await createFixture({ reviews: [running], reviewError: 'no such PR' });
    const host: HTMLElement = fixture.nativeElement;
    expect(host.querySelector('.cr-reviews-table tbody tr mat-spinner')).toBeTruthy();
    expect(host.querySelector('app-inline-banner[variant="error"]')).toBeTruthy();
    await expectNoAxeViolations(host);
  }, 15000);
});
