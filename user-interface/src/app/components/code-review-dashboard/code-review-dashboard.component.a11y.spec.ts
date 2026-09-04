import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { MatTooltip } from '@angular/material/tooltip';
import { vi } from 'vitest';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { CodeReviewDashboardComponent } from './code-review-dashboard.component';
import type { CodeReviewRunItem, GitHubConfigResponse } from '../../models/integrations.model';
import { makePulls, REPO } from './testing/fixtures';
import { expectNoAxeViolations } from '../../testing/a11y';

const CONFIGURED: GitHubConfigResponse = {
  enabled: true,
  token_configured: true,
  default_label: 'ai',
};

describe('CodeReviewDashboardComponent a11y', () => {
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

  beforeEach(() => {
    apiSpy = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      getJobStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running' })),
    };
    integrationsSpy = {
      getGitHubConfig: vi.fn().mockReturnValue(of(CONFIGURED)),
      getGitHubRepos: vi.fn().mockReturnValue(of([REPO])),
      getGitHubPullRequests: vi.fn().mockReturnValue(of(makePulls(3))),
      runGitHubReviewPr: vi.fn(),
      getGitHubReviewHistory: vi.fn().mockReturnValue(of([])),
      createGitHubReviewIssues: vi.fn(),
    };
  });

  async function createFixture(): Promise<ComponentFixture<CodeReviewDashboardComponent>> {
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

    const fixture = TestBed.createComponent(CodeReviewDashboardComponent);
    fixture.detectChanges();
    return fixture;
  }

  it('has no axe violations in the configured state with an expanded repo and PR list', async () => {
    const fixture = await createFixture();
    const component = fixture.componentInstance;
    component.toggleRepo(component.repos[0]);
    fixture.detectChanges();
    // Flush the deferred reviewAnnouncement write (timer(0) in ngOnInit) before the axe
    // pass. Real timers only — vi.useFakeTimers() would also stall axe's own internal
    // async operations and time the test out.
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();

    const host: HTMLElement = fixture.nativeElement;
    // Guard: don't pass axe vacuously against an empty DOM.
    expect(host.querySelector('.cr-repo-row')).toBeTruthy();
    expect(host.querySelector('.cr-repo-pulls')).toBeTruthy();
    expect(host.querySelectorAll('.cr-pull-row').length).toBe(3);
    expect(host.querySelectorAll('[role="status"]').length).toBeGreaterThan(0);
    await expectNoAxeViolations(host);
  }, 15000);

  it('has no axe violations with an expanded PR showing a completed review row', async () => {
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
    const fixture = await createFixture();
    const component = fixture.componentInstance;
    component.toggleRepo(component.repos[0]);
    fixture.detectChanges();
    component.togglePull(component.pulls[0]);
    fixture.detectChanges();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();

    const host: HTMLElement = fixture.nativeElement;
    expect(host.querySelector('.cr-pull-detail')).toBeTruthy();
    expect(host.querySelectorAll('.cr-reviews-table tbody tr').length).toBe(1);
    await expectNoAxeViolations(host);
  }, 15000);

  it('exposes the repo description as a tooltip on the repo row button, with no nested tab stops', async () => {
    const fixture = await createFixture();
    const component = fixture.componentInstance;
    component.toggleRepo(component.repos[0]);
    fixture.detectChanges();

    const rowDebugEl = fixture.debugElement.query(By.css('.cr-repo-row'));
    expect((rowDebugEl.nativeElement as HTMLElement).tagName).toBe('BUTTON');
    // Pinned to the literal description string (not repo.description) so this
    // assertion independently catches a wrongly hoisted tooltip rather than trivially
    // agreeing with whatever the fixture currently returns.
    expect(rowDebugEl.injector.get(MatTooltip).message).toBe('Widget factory');
    expect((rowDebugEl.nativeElement as HTMLElement).querySelectorAll('[tabindex]').length).toBe(0);

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('binds no tooltip message on the repo row button when the repo has no description', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([{ ...REPO, description: null }]));
    const fixture = await createFixture();

    const rowDebugEl = fixture.debugElement.query(By.css('.cr-repo-row'));
    // The button still carries the matTooltip binding (coalesced from null to ''), and
    // MatTooltip's own empty-message handling is what keeps this a no-op tooltip rather
    // than a separate guard on the binding itself.
    expect(rowDebugEl.injector.get(MatTooltip).message).toBe('');
    expect((rowDebugEl.nativeElement as HTMLElement).querySelectorAll('[tabindex]').length).toBe(0);

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);
});
