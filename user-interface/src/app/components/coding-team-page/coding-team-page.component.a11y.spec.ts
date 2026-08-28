import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import type { CodingTeamJobListItem } from '../../models/coding-team.model';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { vi } from 'vitest';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { NotificationService } from '../../core/notification.service';
import { CodingTeamPageComponent } from './coding-team-page.component';
import type { GitHubConfigResponse, GitHubIssueItem, GitHubRepoItem } from '../../models/integrations.model';
import { expectNoAxeViolations } from '../../testing/a11y';

function makeIssues(count: number): GitHubIssueItem[] {
  return Array.from({ length: count }, (_, i) => ({
    number: i + 1,
    title: `Issue ${i + 1}`,
    body_preview: `body ${i + 1}`,
    labels: i % 2 === 0 ? ['bug'] : [],
    html_url: `https://example.com/${i + 1}`,
    dependencies: [],
    open_dependencies: [],
    blocked: false,
  }));
}

const CONFIGURED: GitHubConfigResponse = {
  enabled: true,
  token_configured: true,
  default_label: 'ai',
};

/** The repo the fake PAT can access; the page lists repos and loads issues per repo. */
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

/** A non-terminal GitHub-issue run for this repo. */
function ghRun(overrides: Partial<CodingTeamJobListItem> = {}): CodingTeamJobListItem {
  return {
    job_id: 'j-run',
    status: 'running',
    phase: 'coding',
    status_text: 'writing files',
    updated_at: '2026-06-09T10:00:00Z',
    github_context: { owner: 'acme', repo: 'widgets', issue_number: 2, issue_url: 'https://example.com/2' },
    ...overrides,
  };
}

describe('CodingTeamPageComponent a11y', () => {
  let component: CodingTeamPageComponent;
  let fixture: ComponentFixture<CodingTeamPageComponent>;
  let apiSpy: {
    health: ReturnType<typeof vi.fn>;
    getJobStatus: ReturnType<typeof vi.fn>;
    submitAnswers: ReturnType<typeof vi.fn>;
    listJobs: ReturnType<typeof vi.fn>;
    resumeJob: ReturnType<typeof vi.fn>;
  };
  let integrationsSpy: {
    getGitHubConfig: ReturnType<typeof vi.fn>;
    getGitHubRepos: ReturnType<typeof vi.fn>;
    getGitHubIssues: ReturnType<typeof vi.fn>;
    runGitHubIssue: ReturnType<typeof vi.fn>;
    getGitHubPullRequests: ReturnType<typeof vi.fn>;
    addressPrComments: ReturnType<typeof vi.fn>;
  };
  let notificationsSpy: { saved: ReturnType<typeof vi.fn> };

  async function setup(): Promise<void> {
    await TestBed.configureTestingModule({
      imports: [CodingTeamPageComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: CodingTeamApiService, useValue: apiSpy },
        { provide: IntegrationsApiService, useValue: integrationsSpy },
        { provide: NotificationService, useValue: notificationsSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(CodingTeamPageComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }

  beforeEach(() => {
    localStorage.clear();
    apiSpy = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      getJobStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running' })),
      submitAnswers: vi.fn(),
      listJobs: vi.fn().mockReturnValue(of([])),
      resumeJob: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running', message: '' })),
    };
    integrationsSpy = {
      getGitHubConfig: vi.fn().mockReturnValue(of(CONFIGURED)),
      getGitHubRepos: vi.fn().mockReturnValue(of([REPO])),
      getGitHubIssues: vi.fn().mockReturnValue(of(makeIssues(3))),
      runGitHubIssue: vi.fn(),
      getGitHubPullRequests: vi.fn().mockReturnValue(
        of([
          {
            number: 7,
            title: 'PR 7',
            body_preview: 'body',
            author: 'octocat',
            html_url: 'https://github.com/acme/widgets/pull/7',
            head: 'feature-7',
            base: 'main',
            draft: false,
            labels: [],
            updated_at: '2026-06-09T10:00:00Z',
          },
        ]),
      ),
      addressPrComments: vi.fn(),
    };
    notificationsSpy = { saved: vi.fn() };
  });

  afterEach(() => {
    // Tear down the runs/status poll timers so they never bleed into the next test.
    fixture?.destroy();
    localStorage.clear();
  });

  /** Switch the visible view (the page opens on 'jobs') and re-render. */
  function showView(view: 'chat' | 'github' | 'pulls' | 'jobs'): void {
    component.activeView = view;
    fixture.detectChanges();
  }

  /** Expand the first accessible repo so its issues load (issues are per-repo now). */
  function expandFirstRepo(): void {
    component.toggleRepo(component.repos[0]);
    fixture.detectChanges();
  }

  /**
   * Put a single run into the Jobs accordion and open it with the given status. The run detail
   * renders inside the list's @for, so the run must be present in `runningRuns` for the expanded
   * detail to appear.
   */
  function openRun(run: CodingTeamJobListItem, jobStatus: Record<string, unknown>): void {
    component.runs = [run];
    component.runningRuns = [run];
    component.recentRuns = [];
    component['buildRunVms']();
    component.selectedRunId = run.job_id;
    component.selectedRunNumber = run.github_context?.issue_number ?? null;
    component.jobStatus = jobStatus as never;
    component.activeView = 'jobs';
    fixture.detectChanges();
  }

  it('has no axe violations on the default Jobs view', async () => {
    await setup();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.jobs-panel')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the Chat view', async () => {
    await setup();
    showView('chat');
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-team-assistant-chat')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the GitHub view with a repo expanded and issues loaded', async () => {
    await setup();
    showView('github');
    expandFirstRepo();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelectorAll('.github-issue-row').length).toBe(3);
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the Pull Requests view with a repo expanded and PRs loaded', async () => {
    await setup();
    showView('github');
    expandFirstRepo();
    showView('pulls');
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelectorAll('.pull-row').length).toBe(1);
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the Jobs view with no runs', async () => {
    await setup();
    showView('jobs');
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.jobs-panel')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the Jobs view with a running run open', async () => {
    await setup();
    openRun(ghRun({ status: 'running' }), { job_id: 'j-run', status: 'running', phase: 'coding' });
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-coding-team-monitor')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the Jobs view with a failed run open', async () => {
    await setup();
    openRun(ghRun({ status: 'failed' }), { job_id: 'j-run', status: 'failed' });
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-coding-team-monitor')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);
});
