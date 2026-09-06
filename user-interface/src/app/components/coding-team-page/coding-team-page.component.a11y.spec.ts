import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { Subject, of } from 'rxjs';
import type { CodingTeamJobListItem } from '../../models/coding-team.model';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { MatTooltip } from '@angular/material/tooltip';
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

  it('has no axe violations on the GitHub view with an issue selected (confirm panel open)', async () => {
    await setup();
    showView('github');
    expandFirstRepo();
    component.selectIssue(component.issues[0]);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.github-confirm-panel')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the GitHub view with a blocked issue selected', async () => {
    await setup();
    // Set after setup() (which installs its own default) and before expandFirstRepo() (which
    // triggers the fetch this override needs to win), so this mock can never be clobbered.
    integrationsSpy.getGitHubIssues.mockReturnValue(
      of([
        {
          number: 1,
          title: 'Blocked issue',
          body_preview: 'body',
          labels: [],
          html_url: 'https://example.com/1',
          dependencies: [{ number: 2, title: 'Dep', state: 'open' }],
          open_dependencies: [2],
          blocked: true,
        },
      ]),
    );
    showView('github');
    expandFirstRepo();
    component.selectIssue(component.issues[0]);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-inline-banner[variant="warning"]')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('exposes the composed repo description and issue-count tooltip on the row button, with no nested tab stops', async () => {
    await setup();
    showView('github');
    const el: HTMLElement = fixture.nativeElement;
    const row = el.querySelector('.github-repo-row') as HTMLElement;
    expect(row).not.toBeNull();
    expect(row.tagName).toBe('BUTTON');

    const rowDebugEl = fixture.debugElement.query(By.css('.github-repo-row'));
    const tooltip = rowDebugEl.injector.get(MatTooltip);
    // Pinned to the literal composed string (not component.repoRowTooltip(...)) so this
    // assertion independently catches a wrongly composed tooltip rather than trivially
    // agreeing with whatever the method under test currently returns.
    expect(tooltip.message).toBe('Widget factory — Open issues and pull requests reported by GitHub');

    expect(row.querySelectorAll('[tabindex]').length).toBe(0);

    await expectNoAxeViolations(el);
  }, 15000);

  it('exposes the composed title and in-progress tooltip on the issue row button, with no nested tab stops', async () => {
    await setup();
    showView('github');
    expandFirstRepo();

    // Mark issue #2 as already in progress (matching REPO's owner/name) so both the
    // plain-title and in-progress-clause branches of issueRowTooltip() render in the same list.
    component.activeRunKeys = new Set(['acme/widgets#2']);
    component['recomputeIssueVms']();
    fixture.detectChanges();

    const rows = fixture.debugElement.queryAll(By.css('.github-issue-row'));
    expect(rows.length).toBe(3);
    rows.forEach((row) => {
      expect((row.nativeElement as HTMLElement).querySelectorAll('[tabindex]').length).toBe(0);
    });

    // Pinned to the literal composed strings (not component.issueRowTooltip(...)) so these
    // assertions independently catch a wrongly composed tooltip rather than trivially
    // agreeing with whatever the method under test currently returns.
    expect(rows[0].injector.get(MatTooltip).message).toBe('Issue 1');
    expect(rows[1].injector.get(MatTooltip).message).toBe(
      'Issue 2 — The coding team is already working on this issue'
    );

    const el: HTMLElement = fixture.nativeElement;
    await expectNoAxeViolations(el);
  }, 15000);

  it('exposes the run detail as a tooltip on the run row button, with no nested tab stops, and no tooltip when there is no detail', async () => {
    await setup();
    openRun(ghRun({ status: 'running' }), { job_id: 'j-run', status: 'running', phase: 'coding' });

    const runRowDebugEl = fixture.debugElement.query(By.css('.coding-run-item'));
    expect((runRowDebugEl.nativeElement as HTMLElement).tagName).toBe('BUTTON');
    // Pinned to the literal detail string (not vm.detail) so this assertion independently
    // catches a wrongly hoisted tooltip rather than trivially agreeing with whatever the
    // view-model currently returns.
    expect(runRowDebugEl.injector.get(MatTooltip).message).toBe('writing files');
    expect((runRowDebugEl.nativeElement as HTMLElement).querySelectorAll('[tabindex]').length).toBe(0);

    // A terminal run has no detail — the button still carries the matTooltip binding
    // (coalesced from null to ''), and MatTooltip's own empty-message handling is what
    // keeps this a no-op tooltip rather than a separate guard on the binding itself.
    openRun(ghRun({ status: 'failed' }), { job_id: 'j-run', status: 'failed' });
    const failedRunRowDebugEl = fixture.debugElement.query(By.css('.coding-run-item'));
    expect(failedRunRowDebugEl.injector.get(MatTooltip).message).toBe('');
    expect((failedRunRowDebugEl.nativeElement as HTMLElement).querySelectorAll('[tabindex]').length).toBe(0);

    const el: HTMLElement = fixture.nativeElement;
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the Jobs view with no runs', async () => {
    await setup();
    showView('jobs');
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.jobs-panel')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the Jobs view with a run selected but no status yet ("Starting…")', async () => {
    await setup();
    const run = ghRun({ job_id: 'j-run', status: 'running' });
    component.runs = [run];
    component.runningRuns = [run];
    component.recentRuns = [];
    component['buildRunVms']();
    // toggleRun selects and starts polling on a timer, so jobStatus stays null until the
    // poller's first (async) tick — the hoisted run-detail container's pending branch.
    component.toggleRun(run);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(component.jobStatus).toBeNull();
    expect(el.querySelector('[id="run-detail-j-run"]')).not.toBeNull();
    expect(el.querySelector('app-loading-spinner')).not.toBeNull();
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

  // This is a regression guard on the loading markup, not proof of WCAG 4.1.3 compliance —
  // axe-core has no rule for status-message announcements; see coding-team-page.component.spec.ts
  // for the role="status" DOM assertions that are the actual proof.
  it('has no axe violations on the GitHub view while repositories are loading', async () => {
    const reposSubject = new Subject<GitHubRepoItem[]>();
    integrationsSpy.getGitHubRepos.mockReturnValue(reposSubject.asObservable());
    await setup();
    showView('github');
    const el: HTMLElement = fixture.nativeElement;
    expect(component.loadingRepos).toBe(true);
    expect(el.querySelector('app-loading-spinner')).not.toBeNull();
    await expectNoAxeViolations(el);
    reposSubject.next([REPO]);
    reposSubject.complete();
  }, 15000);

  it('has no axe violations on the GitHub view with no repository access', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(of([]));
    await setup();
    showView('github');
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.github-empty')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  // Regression guards on the filtered markup, not proof of WCAG 4.1.3 — axe-core has no rule for
  // status-message announcements; see coding-team-page.component.spec.ts for the role="status"
  // DOM assertions on the result-count announcers that are the actual proof.
  it('has no axe violations on the GitHub view with a search narrowing the repo and issue lists', async () => {
    await setup();
    showView('github');
    expandFirstRepo();
    component.repoSearch = 'acme'; // still matches the expanded repo, so both filters are active
    component.issueSearch = 'Issue 2';
    component.onIssueSearchChange();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelectorAll('.github-repo-row').length).toBe(1);
    expect(el.querySelectorAll('.github-issue-row').length).toBe(1);
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the GitHub view when a search matches no repositories', async () => {
    await setup();
    showView('github');
    component.repoSearch = 'nonexistent-repo-xyz';
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelectorAll('.github-repo-row').length).toBe(0);
    expect(el.querySelector('.github-empty')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the GitHub view with no open issues', async () => {
    integrationsSpy.getGitHubIssues.mockReturnValue(of([]));
    await setup();
    showView('github');
    expandFirstRepo();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.github-empty')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  // NOTE: MatTooltip's overlay open-on-focus behavior relies on FocusMonitor's
  // keyboard-origin detection, which is not reliably exercisable under jsdom
  // (no real focus/paint pipeline). These tests assert the static, DOM-level
  // preconditions for that behavior — a focusable host element carrying
  // matTooltip/aria-label — not that the tooltip overlay actually opens.
  // Tooltip-opens-on-Tab is verified manually in Chrome (see PR description).

  it('renders the Runs-panel legend as a focusable button with a non-empty accessible name', async () => {
    await setup();
    const el: HTMLElement = fixture.nativeElement;
    const legend = el.querySelector('.jobs-panel__legend');
    expect(legend).not.toBeNull();
    expect(legend?.tagName).toBe('BUTTON');
    const accessibleName = (legend?.getAttribute('aria-label') ?? '').trim();
    expect(accessibleName.length).toBeGreaterThan(0);
    await expectNoAxeViolations(el);
  }, 15000);

  it('exposes full, untruncated task titles on task chips for keyboard and AT users', async () => {
    await setup();
    const longTitle =
      'Refactor the authentication middleware to support pluggable providers across every backend service';
    openRun(ghRun({ status: 'running' }), {
      job_id: 'j-run',
      status: 'running',
      phase: 'coding',
      task_graph_snapshot: [
        { id: 't1', title: longTitle, status: 'in_progress' },
        { id: 't2', title: 'short title', status: 'pending' },
      ],
    });
    const el: HTMLElement = fixture.nativeElement;
    const chips = el.querySelectorAll<HTMLElement>('.github-task-chip');
    expect(chips.length).toBe(2);
    chips.forEach((chip) => {
      expect(chip.tabIndex).toBe(0);
      expect(chip.getAttribute('role')).toBe('img');
    });
    expect(chips[0].getAttribute('aria-label')).toBe(longTitle + ' — in_progress');
    expect(chips[0].textContent).not.toContain(longTitle);
    await expectNoAxeViolations(el);
  }, 15000);
});
