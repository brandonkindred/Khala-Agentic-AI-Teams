import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import type { CodingTeamJobListItem } from '../../models/coding-team.model';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { vi } from 'vitest';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { CodingTeamPageComponent } from './coding-team-page.component';
import type { GitHubConfigResponse, GitHubIssueItem } from '../../models/integrations.model';

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

function issueWith(overrides: Partial<GitHubIssueItem>): GitHubIssueItem {
  return {
    number: 1,
    title: 'Issue 1',
    body_preview: 'body 1',
    labels: [],
    html_url: 'https://example.com/1',
    dependencies: [],
    open_dependencies: [],
    blocked: false,
    ...overrides,
  };
}

const CONFIGURED: GitHubConfigResponse = {
  enabled: true,
  token_configured: true,
  owner: 'acme',
  repo: 'widgets',
  default_label: 'ai',
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

/** Let pending timer(0) emissions (runs poll, then the selected-run status poll) fire. */
async function flushAsync(): Promise<void> {
  for (let i = 0; i < 3; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

describe('CodingTeamPageComponent', () => {
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
    getGitHubIssues: ReturnType<typeof vi.fn>;
    runGitHubIssue: ReturnType<typeof vi.fn>;
  };

  async function setup(): Promise<void> {
    await TestBed.configureTestingModule({
      imports: [CodingTeamPageComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: CodingTeamApiService, useValue: apiSpy },
        { provide: IntegrationsApiService, useValue: integrationsSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(CodingTeamPageComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }

  beforeEach(() => {
    apiSpy = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      getJobStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running' })),
      submitAnswers: vi.fn(),
      listJobs: vi.fn().mockReturnValue(of([])),
      resumeJob: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running', message: '' })),
    };
    integrationsSpy = {
      getGitHubConfig: vi.fn().mockReturnValue(of(CONFIGURED)),
      getGitHubIssues: vi.fn().mockReturnValue(of(makeIssues(3))),
      runGitHubIssue: vi.fn(),
    };
  });

  afterEach(() => {
    // Tear down the runs/status poll timers so they never bleed into the next test.
    fixture?.destroy();
  });

  /** Switch the visible view (the page opens on 'chat') and re-render. */
  function showView(view: 'chat' | 'github' | 'jobs'): void {
    component.activeView = view;
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
    component.selectedRunId = run.job_id;
    component.selectedRunNumber = run.github_context?.issue_number ?? null;
    component.jobStatus = jobStatus as never;
    component.activeView = 'jobs';
    fixture.detectChanges();
  }

  it('should create', async () => {
    await setup();
    expect(component).toBeTruthy();
  });

  it('auto-loads issues on init when GitHub is configured', async () => {
    await setup();
    expect(integrationsSpy.getGitHubConfig).toHaveBeenCalled();
    expect(integrationsSpy.getGitHubIssues).toHaveBeenCalled();
    expect(component.githubConfigured).toBe(true);
    expect(component.issuesLoaded).toBe(true);
    expect(component.issues.length).toBe(3);
  });

  it('polls the Runs list with terminal jobs included (active=false)', async () => {
    await setup();
    await flushAsync();
    expect(apiSpy.listJobs).toHaveBeenCalledWith(false);
  });

  it('does NOT auto-load issues or poll runs when GitHub is not configured', async () => {
    integrationsSpy.getGitHubConfig.mockReturnValue(
      of({ ...CONFIGURED, token_configured: false }),
    );
    await setup();
    await flushAsync();
    expect(component.githubConfigured).toBe(false);
    expect(integrationsSpy.getGitHubIssues).not.toHaveBeenCalled();
    expect(apiSpy.listJobs).not.toHaveBeenCalled();
  });

  it('handles a failed config check without loading issues', async () => {
    integrationsSpy.getGitHubConfig.mockReturnValue(throwError(() => new Error('boom')));
    await setup();
    expect(component.githubConfigured).toBe(false);
    expect(component.loadingConfig).toBe(false);
    expect(integrationsSpy.getGitHubIssues).not.toHaveBeenCalled();
  });

  it('paginates client-side and resets to the first page on (re)load', async () => {
    integrationsSpy.getGitHubIssues.mockReturnValue(of(makeIssues(25)));
    await setup();

    expect(component.pageIndex).toBe(0);
    expect(component.pageSize).toBe(10);
    expect(component.pagedIssues.map((i) => i.number)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);

    component.onPageChange({ pageIndex: 1, pageSize: 10, length: 25 });
    expect(component.pagedIssues.map((i) => i.number)).toEqual([11, 12, 13, 14, 15, 16, 17, 18, 19, 20]);

    component.onPageChange({ pageIndex: 0, pageSize: 25, length: 25 });
    expect(component.pageSize).toBe(25);
    expect(component.pagedIssues.length).toBe(25);

    component.loadIssues();
    expect(component.pageIndex).toBe(0);
  });

  it('surfaces an error when loading issues fails', async () => {
    integrationsSpy.getGitHubIssues.mockReturnValue(
      throwError(() => ({ error: { detail: 'rate limited' } })),
    );
    await setup();
    expect(component.issueError).toBe('rate limited');
    expect(component.loadingIssues).toBe(false);
  });

  it('selects an issue, cancels, then runs it — selecting the new run, no dismiss', async () => {
    await setup();
    const issue = component.issues[0];

    component.selectIssue(issue);
    expect(component.selectedIssue).toBe(issue);

    component.cancelSelection();
    expect(component.selectedIssue).toBeNull();

    integrationsSpy.runGitHubIssue.mockReturnValue(
      of({ job_id: 'j1', issue_number: issue.number, issue_url: 'u', status: 'queued', message: '' }),
    );
    component.selectIssue(issue);
    component.confirmAndRun();
    expect(integrationsSpy.runGitHubIssue).toHaveBeenCalledWith({ issue_number: issue.number });
    expect(component.selectedRunId).toBe('j1');
    expect(component.selectedRunNumber).toBe(issue.number);
    expect(component.selectedIssue).toBeNull();
    expect(component.isIssueInProgress(issue)).toBe(true);
    // The panel is not dismissable.
    expect((component as unknown as Record<string, unknown>)['dismissJob']).toBeUndefined();
  });

  it('surfaces an error when starting a run fails', async () => {
    await setup();
    const issue = component.issues[0];
    integrationsSpy.runGitHubIssue.mockReturnValue(throwError(() => ({ error: { detail: 'duplicate run' } })));
    component.selectIssue(issue);
    component.confirmAndRun();
    expect(component.issueError).toBe('duplicate run');
    expect(component.runningIssue).toBe(false);
  });

  it('treats completed_with_failures as terminal (so polling stops)', async () => {
    await setup();
    component.jobStatus = null;
    expect(component.isJobTerminal()).toBe(false);
    component.jobStatus = { job_id: 'j1', status: 'running' };
    expect(component.isJobTerminal()).toBe(false);
    for (const status of ['completed', 'completed_with_failures', 'failed', 'cancelled']) {
      component.jobStatus = { job_id: 'j1', status };
      expect(component.isJobTerminal()).toBe(true);
    }
  });

  it('confirmAndRun is a no-op without a selected issue', async () => {
    await setup();
    component.selectedIssue = null;
    component.confirmAndRun();
    expect(integrationsSpy.runGitHubIssue).not.toHaveBeenCalled();
  });

  it('records the latest job id when a workflow is launched', async () => {
    await setup();
    component.onWorkflowLaunched({ job_id: 'wf-1', conversation_id: 'c1' });
    expect(component.latestJobId).toBe('wf-1');
  });

  // -------------------------------------------------------------------------
  // View switcher (Chat / GitHub / Jobs)
  // -------------------------------------------------------------------------

  describe('view switcher', () => {
    it('opens on the Chat view showing the assistant, not the GitHub or Jobs panels', async () => {
      await setup();
      expect(component.activeView).toBe('chat');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('app-team-assistant-chat')).not.toBeNull();
      expect(el.querySelector('.github-section')).toBeNull();
      expect(el.querySelector('.jobs-panel')).toBeNull();
    });

    it('shows only the GitHub issues panel when the GitHub view is active', async () => {
      await setup();
      showView('github');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelectorAll('.github-issue-row').length).toBe(3);
      expect(el.querySelector('app-team-assistant-chat')).toBeNull();
      expect(el.querySelector('.jobs-panel')).toBeNull();
    });

    it('shows only the Jobs panel when the Jobs view is active', async () => {
      await setup();
      showView('jobs');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.jobs-panel')).not.toBeNull();
      expect(el.querySelector('app-team-assistant-chat')).toBeNull();
      expect(el.querySelector('.github-issue-row')).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // Pure helpers
  // -------------------------------------------------------------------------

  describe('helpers', () => {
    it('maps job statuses to shared badge modifiers', async () => {
      await setup();
      expect(component.badgeClass('running')).toBe('running');
      expect(component.badgeClass('pending')).toBe('running');
      expect(component.badgeClass('completed')).toBe('completed');
      expect(component.badgeClass('failed')).toBe('failed');
      expect(component.badgeClass('cancelled')).toBe('cancelled');
      expect(component.badgeClass('completed_with_failures')).toBe('warning');
      expect(component.badgeClass('waiting_for_user')).toBe('warning');
      expect(component.badgeClass('weird')).toBe('neutral');
      expect(component.badgeClass(undefined)).toBe('neutral');
    });

    it('formats relative times', async () => {
      await setup();
      expect(component.timeAgo()).toBe('');
      expect(component.timeAgo(new Date().toISOString())).toBe('just now');
      expect(component.timeAgo(new Date(Date.now() - 5 * 60000).toISOString())).toBe('5m ago');
      expect(component.timeAgo(new Date(Date.now() - 2 * 3600000).toISOString())).toBe('2h ago');
      expect(component.timeAgo(new Date(Date.now() - 3 * 86400000).toISOString())).toBe('3d ago');
    });

    it('copies the selected job id and flashes confirmation', async () => {
      await setup();
      // No selection → no-op, no throw.
      expect(() => component.copyJobId()).not.toThrow();
      expect(component.jobIdCopied).toBe(false);
      component.selectedRunId = 'abcdef123456';
      component.copyJobId();
      expect(component.jobIdCopied).toBe(true);
    });

    it('swallows a rejected clipboard write instead of leaking an unhandled rejection', async () => {
      await setup();
      const writeText = vi.fn().mockRejectedValue(new Error('permission denied'));
      const original = (navigator as { clipboard?: unknown }).clipboard;
      Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
      try {
        component.selectedRunId = 'abcdef123456';
        expect(() => component.copyJobId()).not.toThrow();
        expect(writeText).toHaveBeenCalledWith('abcdef123456');
        expect(component.jobIdCopied).toBe(true);
        // Let the rejected promise settle; the .catch() must absorb it.
        await flushAsync();
      } finally {
        Object.defineProperty(navigator, 'clipboard', { value: original, configurable: true });
      }
    });

    it('treats a run as waiting only while it is non-terminal', async () => {
      await setup();
      expect(component.isRunActive(ghRun({ status: 'running' }))).toBe(true);
      expect(component.isRunActive(ghRun({ status: 'completed' }))).toBe(false);
      // A run actively paused on questions is "needs answers"…
      expect(
        component.isRunWaiting(ghRun({ status: 'waiting_for_user', waiting_for_answers: true })),
      ).toBe(true);
      // …but a terminal run carrying a stale waiting flag is not.
      expect(
        component.isRunWaiting(ghRun({ status: 'completed', waiting_for_answers: true })),
      ).toBe(false);
      expect(component.isRunWaiting(ghRun({ status: 'running', waiting_for_answers: false }))).toBe(false);
    });

    it('splits runs into running and recent (derived in applyRuns)', async () => {
      await setup();
      component['initialRunsLoad'] = false;
      component['applyRuns']([
        ghRun({ job_id: 'a', status: 'running' }),
        ghRun({ job_id: 'b', status: 'completed', github_context: { owner: 'acme', repo: 'widgets', issue_number: 3 } }),
        ghRun({ job_id: 'c', status: 'waiting_for_user', github_context: { owner: 'acme', repo: 'widgets', issue_number: 4 } }),
      ]);
      expect(component.runningRuns.map((r) => r.job_id)).toEqual(['a', 'c']);
      expect(component.recentRuns.map((r) => r.job_id)).toEqual(['b']);
    });
  });

  // -------------------------------------------------------------------------
  // Issue dependency indicator
  // -------------------------------------------------------------------------

  describe('dependency helpers', () => {
    it('builds blocked and met tooltip / open-ref text', async () => {
      await setup();
      expect(component.hasDependencies(issueWith({ dependencies: [] }))).toBe(false);
      const blocked = issueWith({
        blocked: true,
        open_dependencies: [3, 5],
        dependencies: [
          { number: 3, title: 'A', state: 'open' },
          { number: 5, title: 'B', state: 'open' },
        ],
      });
      expect(component.hasDependencies(blocked)).toBe(true);
      expect(component.openDepRefs(blocked)).toBe('#3, #5');
      expect(component.dependencyTooltip(blocked)).toBe('Blocked by #3, #5 — must be closed first');

      const met = issueWith({
        blocked: false,
        open_dependencies: [],
        dependencies: [
          { number: 3, title: 'A', state: 'closed' },
          { number: 5, title: 'B', state: 'closed' },
        ],
      });
      expect(component.dependencyTooltip(met)).toBe('Depends on #3, #5 (all complete)');
      expect(component.openDepRefs(met)).toBe('');
    });
  });

  describe('dependency indicator rendering', () => {
    it('renders a blocked indicator with the open-dependency count', async () => {
      integrationsSpy.getGitHubIssues.mockReturnValue(
        of([
          issueWith({
            number: 7,
            blocked: true,
            open_dependencies: [3, 5],
            dependencies: [
              { number: 3, title: 'A', state: 'open' },
              { number: 5, title: 'B', state: 'open' },
            ],
          }),
        ]),
      );
      await setup();
      showView('github');
      const el: HTMLElement = fixture.nativeElement;
      const deps = el.querySelector('.github-issue-row__deps');
      expect(deps).not.toBeNull();
      expect(deps?.classList.contains('github-issue-row__deps--blocked')).toBe(true);
      expect(deps?.querySelector('mat-icon')?.textContent?.trim()).toBe('block');
      expect(el.querySelector('.github-issue-row__deps-count')?.textContent?.trim()).toBe('2');
      expect(deps?.getAttribute('role')).toBe('img');
      expect(deps?.getAttribute('aria-label')).toBe('Blocked by #3, #5 — must be closed first');
      expect(deps?.querySelector('mat-icon')?.getAttribute('aria-hidden')).toBe('true');
    });

    it('renders a muted "dependencies met" indicator with no count', async () => {
      integrationsSpy.getGitHubIssues.mockReturnValue(
        of([
          issueWith({
            number: 8,
            blocked: false,
            open_dependencies: [],
            dependencies: [{ number: 3, title: 'A', state: 'closed' }],
          }),
        ]),
      );
      await setup();
      showView('github');
      const el: HTMLElement = fixture.nativeElement;
      const deps = el.querySelector('.github-issue-row__deps');
      expect(deps).not.toBeNull();
      expect(deps?.classList.contains('github-issue-row__deps--met')).toBe(true);
      expect(deps?.querySelector('mat-icon')?.textContent?.trim()).toBe('account_tree');
      expect(el.querySelector('.github-issue-row__deps-count')).toBeNull();
    });

    it('renders no indicator when an issue has no dependencies', async () => {
      integrationsSpy.getGitHubIssues.mockReturnValue(of([issueWith({ number: 9 })]));
      await setup();
      showView('github');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.github-issue-row__deps')).toBeNull();
    });

    it('warns on the inline confirmation for a blocked issue but keeps Confirm enabled', async () => {
      const blocked = issueWith({
        number: 7,
        blocked: true,
        open_dependencies: [3],
        dependencies: [{ number: 3, title: 'A', state: 'open' }],
      });
      integrationsSpy.getGitHubIssues.mockReturnValue(of([blocked]));
      await setup();
      component.activeView = 'github';

      component.selectIssue(blocked);
      fixture.detectChanges();

      const el: HTMLElement = fixture.nativeElement;
      const warning = el.querySelector('.github-confirm-panel__warning');
      expect(warning).not.toBeNull();
      expect(warning?.textContent).toContain('#3');

      const confirmBtn = el.querySelector('.github-confirm-panel__actions button') as HTMLButtonElement;
      expect(confirmBtn.disabled).toBe(false);
    });

    it('keeps the issue list visible and shows the inline confirm under the selected row', async () => {
      await setup();
      component.activeView = 'github';
      component.selectIssue(component.issues[1]); // issue #2
      fixture.detectChanges();
      const el: HTMLElement = fixture.nativeElement;
      // All three issue rows remain visible — selecting one never hides the list.
      expect(el.querySelectorAll('.github-issue-row').length).toBe(3);
      const confirm = el.querySelector('.github-confirm-panel');
      expect(confirm).not.toBeNull();
      expect(confirm?.textContent).toContain('#2');
    });
  });

  // -------------------------------------------------------------------------
  // Selected-run detail rendering
  // -------------------------------------------------------------------------

  describe('selected-run detail', () => {
    function showRun(jobStatusOverrides: Record<string, unknown>): void {
      openRun(
        ghRun({ job_id: 'j1', status: 'waiting_for_user', github_context: { owner: 'acme', repo: 'widgets', issue_number: 5 } }),
        { job_id: 'j1', status: 'waiting_for_user', ...jobStatusOverrides },
      );
    }

    const QUESTION = {
      id: 'q1',
      question_text: 'Which auth flow?',
      options: [{ id: 'oauth', label: 'OAuth' }],
      required: true,
      source: 'tech_lead',
    };

    it('renders the Agent thinking panel when jobStatus.thinking is present', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), { job_id: 'j1', status: 'running', thinking: 'weighing the approach' });
      const stream = fixture.nativeElement.querySelector('.thinking-stream');
      expect(stream).not.toBeNull();
      expect(stream?.textContent).toContain('weighing the approach');
    });

    it('hides the Agent thinking panel when there is no thinking text', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), { job_id: 'j1', status: 'running' });
      expect(fixture.nativeElement.querySelector('.thinking-stream')).toBeNull();
    });

    it('renders the questions panel and waiting banner when the run is paused', async () => {
      await setup();
      showRun({ waiting_for_answers: true, pending_questions: [QUESTION] });
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.github-banner--waiting')).not.toBeNull();
      expect(el.querySelector('app-pending-questions')).not.toBeNull();
      expect(el.textContent).toContain('Which auth flow?');
    });

    it('hides the panel when the run is not waiting for answers', async () => {
      await setup();
      showRun({ waiting_for_answers: false, pending_questions: [QUESTION] });
      expect(fixture.nativeElement.querySelector('app-pending-questions')).toBeNull();
    });

    it('hides the panel when there are no pending questions', async () => {
      await setup();
      showRun({ waiting_for_answers: true, pending_questions: [] });
      expect(fixture.nativeElement.querySelector('app-pending-questions')).toBeNull();
      expect(component.hasPendingQuestions()).toBe(false);
    });

    it('shows the waiting badge in the detail header while paused', async () => {
      await setup();
      showRun({ waiting_for_answers: true, pending_questions: [QUESTION] });
      const badge = fixture.nativeElement.querySelector('.run-detail__header .kh-badge--warning');
      expect(badge?.textContent).toContain('waiting for answers');
    });

    it('offers a "Run again" affordance on a terminal run', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1', status: 'failed' }), { job_id: 'j1', status: 'failed' });
      const retry = fixture.nativeElement.querySelector('.run-detail__retry button');
      expect(retry?.textContent).toContain('Run again');
    });

    it('renders a status modifier class for every task chip in the run detail', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), {
        job_id: 'j1',
        status: 'running',
        task_graph_snapshot: [
          { id: 't1', title: 'Build', status: 'completed' },
          { id: 't2', title: 'Wire API', status: 'failed' },
        ],
      });
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.github-task-chip--completed')).not.toBeNull();
      expect(el.querySelector('.github-task-chip--failed')).not.toBeNull();
    });

    it('onAnswersSubmitted folds the post-submit status in and restarts polling', async () => {
      await setup();
      component.selectedRunId = 'j1';
      component['startPolling']('j1');
      const staleSub = component['pollSub'];
      component.issueError = 'Lost connection to the coding team — status polling failed.';

      const resumed = { job_id: 'j1', status: 'running', waiting_for_answers: false };
      component.onAnswersSubmitted(resumed as never);

      expect(component.jobStatus).toEqual(resumed);
      expect(staleSub?.closed).toBe(true);
      expect(component['pollSub']).not.toBeNull();
      expect(component['pollSub']).not.toBe(staleSub);
      expect(component.issueError).toBeNull();
      component['stopPolling']();
    });
  });

  // -------------------------------------------------------------------------
  // Resume
  // -------------------------------------------------------------------------

  describe('resume', () => {
    it('is a no-op without a selected run', async () => {
      await setup();
      component.selectedRunId = null;
      component.resumeJob();
      expect(apiSpy.resumeJob).not.toHaveBeenCalled();
    });

    it('restarts polling on success', async () => {
      await setup();
      component.selectedRunId = 'j1';
      component.resumeJob();
      expect(apiSpy.resumeJob).toHaveBeenCalledWith('j1');
      expect(component.resumingJob).toBe(false);
      expect(component['pollSub']).not.toBeNull();
      component['stopPolling']();
    });

    it('surfaces an error on failure', async () => {
      await setup();
      component.selectedRunId = 'j1';
      apiSpy.resumeJob.mockReturnValue(throwError(() => ({ error: { detail: 'cannot resume' } })));
      component.resumeJob();
      expect(component.issueError).toBe('cannot resume');
      expect(component.resumingJob).toBe(false);
    });
  });

  // -------------------------------------------------------------------------
  // Runs panel — list, selection, restore, chips
  // -------------------------------------------------------------------------

  describe('runs panel', () => {
    it('renders an empty state when there are no runs', async () => {
      await setup();
      await flushAsync();
      showView('jobs');
      expect(fixture.nativeElement.querySelector('.runs-panel__empty')).not.toBeNull();
    });

    it('renders Running and Recent sections without a delete button', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({ job_id: 'r1', status: 'running' }),
          ghRun({ job_id: 'r2', status: 'completed', github_context: { owner: 'acme', repo: 'widgets', issue_number: 3 } }),
        ]),
      );
      await setup();
      await flushAsync();
      showView('jobs');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelectorAll('.coding-run-item').length).toBe(2);
      expect(el.querySelector('.delete-btn')).toBeNull();
      expect(el.textContent).toContain('Running');
      expect(el.textContent).toContain('Recent');
    });

    it('shows a needs-answers badge on a running run paused on questions', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({
            job_id: 'paused',
            status: 'waiting_for_user',
            waiting_for_answers: true,
            github_context: { owner: 'acme', repo: 'widgets', issue_number: 9 },
          }),
        ]),
      );
      await setup();
      await flushAsync();
      showView('jobs');
      expect(fixture.nativeElement.textContent).toContain('needs answers');
    });

    it('never shows a needs-answers badge or a live detail line on a terminal Recent run', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({
            job_id: 'done',
            status: 'completed',
            // Stale flag from a run that was paused before it finished.
            waiting_for_answers: true,
            status_text: 'wrote files',
            github_context: { owner: 'acme', repo: 'widgets', issue_number: 8 },
          }),
        ]),
      );
      await setup();
      await flushAsync();
      showView('jobs');
      const el: HTMLElement = fixture.nativeElement;
      // Terminal runs are never auto-selected, so only the Recent row is in the DOM.
      expect(el.textContent).not.toContain('needs answers');
      expect(el.querySelector('.coding-run-item__detail')).toBeNull();
      expect(el.querySelector('.kh-badge--completed')?.textContent).toContain('completed');
    });

    it('expands the auto-selected run inline in the Jobs accordion', async () => {
      apiSpy.listJobs.mockReturnValue(of([ghRun({ job_id: 'r1', github_context: { owner: 'acme', repo: 'widgets', issue_number: 1 } })]));
      await setup();
      await flushAsync();
      showView('jobs');
      expect(component.selectedRunId).toBe('r1');
      const el: HTMLElement = fixture.nativeElement;
      // The auto-selected run's row is marked selected and its detail is expanded beneath it.
      expect(el.querySelector('.coding-run-item.selected')).not.toBeNull();
      expect(el.querySelector('.run-detail')).not.toBeNull();
    });

    it('toggles a run row open then collapsed in the accordion', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([ghRun({ job_id: 'r1', status: 'completed', github_context: { owner: 'acme', repo: 'widgets', issue_number: 1 } })]),
      );
      await setup();
      await flushAsync();
      showView('jobs');
      const run = component.recentRuns[0];
      // Terminal run is not auto-selected, so nothing is expanded yet.
      expect(component.selectedRunId).toBeNull();

      component.toggleRun(run);
      await flushAsync();
      fixture.detectChanges();
      expect(component.selectedRunId).toBe('r1');
      expect(fixture.nativeElement.querySelector('.run-detail')).not.toBeNull();

      component.toggleRun(run);
      fixture.detectChanges();
      expect(component.selectedRunId).toBeNull();
      expect(component.jobStatus).toBeNull();
      expect(fixture.nativeElement.querySelector('.run-detail')).toBeNull();
    });

    it('auto-selects a non-terminal run on first load and starts polling it', async () => {
      apiSpy.listJobs.mockReturnValue(of([ghRun({ job_id: 'j-restore' })]));
      await setup();
      await flushAsync();
      expect(apiSpy.listJobs).toHaveBeenCalledWith(false);
      expect(component.selectedRunId).toBe('j-restore');
      expect(component.selectedRunNumber).toBe(2);
      expect(apiSpy.getJobStatus).toHaveBeenCalledWith('j-restore');
      expect(component.activeIssueNumbers.has(2)).toBe(true);
    });

    it('prefers a run paused on questions over a more-recent running run', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({ job_id: 'fresh', status: 'running', updated_at: '2026-06-09T12:00:00Z', github_context: { owner: 'acme', repo: 'widgets', issue_number: 4 } }),
          ghRun({ job_id: 'paused', status: 'waiting_for_user', waiting_for_answers: true, updated_at: '2026-06-09T09:00:00Z', github_context: { owner: 'acme', repo: 'widgets', issue_number: 5 } }),
        ]),
      );
      await setup();
      await flushAsync();
      expect(component.selectedRunId).toBe('paused');
    });

    it('picks the most recently updated run when several are active', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({ job_id: 'older', updated_at: '2026-06-08T10:00:00Z' }),
          ghRun({ job_id: 'newer', updated_at: '2026-06-09T11:00:00Z', github_context: { owner: 'acme', repo: 'widgets', issue_number: 3 } }),
        ]),
      );
      await setup();
      await flushAsync();
      expect(component.selectedRunId).toBe('newer');
      expect(component.activeIssueNumbers).toEqual(new Set([2, 3]));
    });

    it('ignores runs belonging to a different repository', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({ github_context: { owner: 'acme', repo: 'other-repo', issue_number: 2 } }),
          ghRun({ job_id: 'other-owner', github_context: { owner: 'someone-else', repo: 'widgets', issue_number: 2 } }),
        ]),
      );
      await setup();
      await flushAsync();
      expect(component.selectedRunId).toBeNull();
      expect(component.runs.length).toBe(0);
      expect(component.activeIssueNumbers.size).toBe(0);
    });

    it('matches the configured repository case-insensitively', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([ghRun({ github_context: { owner: 'Acme', repo: 'Widgets', issue_number: 2 } })]),
      );
      await setup();
      await flushAsync();
      expect(component.selectedRunId).toBe('j-run');
      expect(component.activeIssueNumbers.has(2)).toBe(true);
    });

    it('lists a terminal run under Recent without auto-selecting it', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({ job_id: 'done', status: 'completed' }),
          { job_id: 'local-run', status: 'running', repo_path: '/tmp/x' },
        ]),
      );
      await setup();
      await flushAsync();
      expect(component.selectedRunId).toBeNull();
      expect(component.recentRuns.map((r) => r.job_id)).toEqual(['done']);
      expect(component.activeIssueNumbers.size).toBe(0);
    });

    it('stays usable when the runs list cannot be fetched', async () => {
      apiSpy.listJobs.mockReturnValue(throwError(() => ({ error: { detail: 'down' } })));
      await setup();
      await flushAsync();
      expect(component.selectedRunId).toBeNull();
      expect(component.runsError).toBe('down');
      expect(component.githubConfigured).toBe(true);
    });

    it('does not adopt a run when the list resolves after destroy', async () => {
      apiSpy.listJobs.mockReturnValue(of([ghRun()]));
      await setup();
      fixture.destroy();
      await flushAsync();
      expect(component.selectedRunId).toBeNull();
      expect(component.runs.length).toBe(0);
    });

    it('renders an "In progress" chip on issues with an active run', async () => {
      apiSpy.listJobs.mockReturnValue(of([ghRun({ github_context: { owner: 'acme', repo: 'widgets', issue_number: 2 } })]));
      await setup();
      await flushAsync();
      showView('github');
      const chips = fixture.nativeElement.querySelectorAll('.github-label-chip--active');
      expect(chips.length).toBe(1);
      expect(chips[0].textContent).toContain('In progress');
    });

    it('selectRun is a no-op when the run is already selected', async () => {
      await setup();
      component.selectedRunId = 'x';
      const spy = vi.spyOn(component as unknown as { startPolling: (id: string) => void }, 'startPolling');
      component.selectRun('x');
      expect(spy).not.toHaveBeenCalled();
    });

    it('selectRun selects, derives the issue number, and starts status polling', async () => {
      await setup();
      component.runs = [ghRun({ job_id: 'r9', github_context: { owner: 'acme', repo: 'widgets', issue_number: 9 } })];
      component.selectRun('r9');
      expect(component.selectedRunId).toBe('r9');
      expect(component.selectedRunNumber).toBe(9);
      await flushAsync();
      expect(apiSpy.getJobStatus).toHaveBeenCalledWith('r9');
    });

    it('a stale snapshot cannot wipe the chip of a just-started, still-running run', async () => {
      await setup();
      component.selectedRunId = 'mine';
      component.selectedRunNumber = 9;
      component.jobStatus = { job_id: 'mine', status: 'running' };
      component['applyRuns']([]);
      expect(component.activeIssueNumbers.has(9)).toBe(true);
    });

    it('does not re-add the chip for a selected run that has finished', async () => {
      await setup();
      component.selectedRunId = 'mine';
      component.selectedRunNumber = 9;
      component.jobStatus = { job_id: 'mine', status: 'completed' };
      component['applyRuns']([]);
      expect(component.activeIssueNumbers.has(9)).toBe(false);
    });

    it('drops the chip once the snapshot reports the selected run terminal, even if the polled status is stale', async () => {
      await setup();
      component['initialRunsLoad'] = false;
      component.selectedRunId = 'r1';
      component.selectedRunNumber = 5;
      // Polled status lags behind the server: still "running" though the run has finished.
      component.jobStatus = { job_id: 'r1', status: 'running' };
      component['applyRuns']([
        ghRun({ job_id: 'r1', status: 'completed', github_context: { owner: 'acme', repo: 'widgets', issue_number: 5 } }),
      ]);
      // The fresh snapshot is trusted: #5 is no longer in progress and the run sits under Recent.
      expect(component.activeIssueNumbers.has(5)).toBe(false);
      expect(component.recentRuns.map((r) => r.job_id)).toEqual(['r1']);
    });

    it('drops the chip and refreshes the list when the poller observes a terminal status', async () => {
      await setup();
      component.selectedRunId = 'j1';
      component.selectedRunNumber = 7;
      component.activeIssueNumbers.add(7);
      apiSpy.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'completed' }));
      apiSpy.listJobs.mockReturnValue(of([]));
      component['startPolling']('j1');
      await flushAsync();
      expect(component.jobStatus?.status).toBe('completed');
      expect(component.activeIssueNumbers.has(7)).toBe(false);
    });

    it('discards a stale poll for a run the user switched away from', async () => {
      await setup();
      component.selectedRunId = 'b';
      component.jobStatus = null;
      apiSpy.getJobStatus.mockReturnValue(of({ job_id: 'a', status: 'running' }));
      component['startPolling']('a');
      await flushAsync();
      expect(component.jobStatus).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // Retry
  // -------------------------------------------------------------------------

  describe('retry', () => {
    it('is a no-op when the selected run has no issue number', async () => {
      await setup();
      component.selectedRunNumber = null;
      component.jobStatus = null;
      component.retrySelectedRun();
      expect(integrationsSpy.runGitHubIssue).not.toHaveBeenCalled();
    });

    it('re-runs the selected run\'s issue and selects the new run', async () => {
      await setup();
      component.selectedRunNumber = 5;
      integrationsSpy.runGitHubIssue.mockReturnValue(
        of({ job_id: 'j-retry', issue_number: 5, issue_url: 'u', status: 'queued', message: '' }),
      );
      component.retrySelectedRun();
      expect(integrationsSpy.runGitHubIssue).toHaveBeenCalledWith({ issue_number: 5 });
      expect(component.selectedRunId).toBe('j-retry');
      expect(component.isIssueInProgress(issueWith({ number: 5 }))).toBe(true);
    });

    it('surfaces an error when the retry fails', async () => {
      await setup();
      component.jobStatus = { job_id: 'j1', status: 'failed', github_context: { owner: 'acme', repo: 'widgets', issue_number: 6 } };
      integrationsSpy.runGitHubIssue.mockReturnValue(throwError(() => ({ error: { detail: 'nope' } })));
      component.retrySelectedRun();
      expect(integrationsSpy.runGitHubIssue).toHaveBeenCalledWith({ issue_number: 6 });
      expect(component.issueError).toBe('nope');
      expect(component.runningIssue).toBe(false);
    });
  });

  // -------------------------------------------------------------------------
  // Polling lifecycle
  // -------------------------------------------------------------------------

  describe('polling lifecycle', () => {
    it('re-polls the runs list on the recurring interval, not just once', async () => {
      vi.useFakeTimers();
      try {
        await setup();
        // The initial timer(0) emission fires the first fetch.
        await vi.advanceTimersByTimeAsync(0);
        const afterFirst = apiSpy.listJobs.mock.calls.length;
        expect(afterFirst).toBeGreaterThanOrEqual(1);
        // Advancing one poll interval (RUNS_POLL_MS = 15000ms) triggers a second fetch.
        await vi.advanceTimersByTimeAsync(15000);
        expect(apiSpy.listJobs.mock.calls.length).toBeGreaterThan(afterFirst);
      } finally {
        vi.useRealTimers();
      }
    });

    it('completes the runs refresh trigger and stops status polling on destroy', async () => {
      await setup();
      component.selectedRunId = 'j1';
      component['startPolling']('j1');
      expect(component['pollSub']).not.toBeNull();

      fixture.destroy();

      // Subscribing to a completed Subject invokes complete() synchronously.
      let completed = false;
      component['refreshTrigger$'].subscribe({ complete: () => { completed = true; } });
      expect(completed).toBe(true);
      expect(component['pollSub']).toBeNull();
    });

    it('cancels the copy-confirmation timer on destroy', async () => {
      await setup();
      component.selectedRunId = 'abcdef123456';
      component.copyJobId();
      expect(component.jobIdCopied).toBe(true);
      // The reset timer is tracked so it can be torn down with the component.
      expect(component['copyResetTimer']).not.toBeNull();
      fixture.destroy();
      expect(component['copyResetTimer']).toBeNull();
    });
  });
});
