import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Subject, of, throwError } from 'rxjs';
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

describe('CodingTeamPageComponent', () => {
  let component: CodingTeamPageComponent;
  let fixture: ComponentFixture<CodingTeamPageComponent>;
  let apiSpy: {
    health: ReturnType<typeof vi.fn>;
    getJobStatus: ReturnType<typeof vi.fn>;
    submitAnswers: ReturnType<typeof vi.fn>;
    listJobs: ReturnType<typeof vi.fn>;
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
    };
    integrationsSpy = {
      getGitHubConfig: vi.fn().mockReturnValue(of(CONFIGURED)),
      getGitHubIssues: vi.fn().mockReturnValue(of(makeIssues(3))),
      runGitHubIssue: vi.fn(),
    };
  });

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

  it('does NOT auto-load issues when GitHub is not configured', async () => {
    integrationsSpy.getGitHubConfig.mockReturnValue(
      of({ ...CONFIGURED, token_configured: false }),
    );
    await setup();
    expect(component.githubConfigured).toBe(false);
    expect(integrationsSpy.getGitHubIssues).not.toHaveBeenCalled();
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

    // Defaults to first page of PAGE_SIZE_OPTIONS[0] (10) items.
    expect(component.pageIndex).toBe(0);
    expect(component.pageSize).toBe(10);
    expect(component.pagedIssues.map((i) => i.number)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);

    // Move to the second page.
    component.onPageChange({ pageIndex: 1, pageSize: 10, length: 25 });
    expect(component.pagedIssues.map((i) => i.number)).toEqual([11, 12, 13, 14, 15, 16, 17, 18, 19, 20]);

    // Changing page size is honoured.
    component.onPageChange({ pageIndex: 0, pageSize: 25, length: 25 });
    expect(component.pageSize).toBe(25);
    expect(component.pagedIssues.length).toBe(25);

    // Reloading resets back to the first page.
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

  it('selects, cancels, and runs an issue', async () => {
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
    expect(component.activeJob?.job_id).toBe('j1');
    expect(component.selectedIssue).toBeNull();

    component.dismissJob();
    expect(component.activeJob).toBeNull();
    expect(component.jobStatus).toBeNull();
  });

  it('treats completed_with_failures as terminal (so polling stops and the job is dismissable)', async () => {
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
      // Only open deps are listed in the ref string.
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
      const el: HTMLElement = fixture.nativeElement;
      const deps = el.querySelector('.github-issue-row__deps');
      expect(deps).not.toBeNull();
      expect(deps?.classList.contains('github-issue-row__deps--blocked')).toBe(true);
      expect(deps?.querySelector('mat-icon')?.textContent?.trim()).toBe('block');
      expect(el.querySelector('.github-issue-row__deps-count')?.textContent?.trim()).toBe('2');
      // a11y: the indicator is a single labelled graphic; the icon ligature/count are
      // not announced separately.
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
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.github-issue-row__deps')).toBeNull();
    });

    it('warns on the confirmation panel for a blocked issue but keeps Confirm enabled', async () => {
      const blocked = issueWith({
        number: 7,
        blocked: true,
        open_dependencies: [3],
        dependencies: [{ number: 3, title: 'A', state: 'open' }],
      });
      integrationsSpy.getGitHubIssues.mockReturnValue(of([blocked]));
      await setup();

      component.selectIssue(blocked);
      fixture.detectChanges();

      const el: HTMLElement = fixture.nativeElement;
      const warning = el.querySelector('.github-confirm-panel__warning');
      expect(warning).not.toBeNull();
      expect(warning?.textContent).toContain('#3');

      const confirmBtn = el.querySelector('.github-confirm-panel__actions button') as HTMLButtonElement;
      expect(confirmBtn.disabled).toBe(false);
    });
  });

  it('renders the Agent thinking panel when jobStatus.thinking is present', async () => {
    await setup();
    component.activeJob = {
      job_id: 'j1',
      issue_number: 5,
      issue_url: 'u',
      status: 'queued',
      message: '',
    };
    component.jobStatus = { job_id: 'j1', status: 'running', thinking: 'weighing the approach' };
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const stream = el.querySelector('.thinking-stream');
    expect(stream).not.toBeNull();
    expect(stream?.textContent).toContain('weighing the approach');
  });

  it('hides the Agent thinking panel when there is no thinking text', async () => {
    await setup();
    component.activeJob = {
      job_id: 'j1',
      issue_number: 5,
      issue_url: 'u',
      status: 'queued',
      message: '',
    };
    component.jobStatus = { job_id: 'j1', status: 'running' };
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.thinking-stream')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Pending questions (human-in-the-loop)
  // -------------------------------------------------------------------------

  describe('pending questions panel', () => {
    const QUESTION = {
      id: 'q1',
      question_text: 'Which auth flow?',
      options: [{ id: 'oauth', label: 'OAuth' }],
      required: true,
      source: 'tech_lead',
    };

    function showActiveJob(jobStatusOverrides: Record<string, unknown>): void {
      component.activeJob = { job_id: 'j1', issue_number: 5, issue_url: 'u', status: 'queued', message: '' };
      component.jobStatus = { job_id: 'j1', status: 'waiting_for_user', ...jobStatusOverrides } as any;
      fixture.detectChanges();
    }

    it('renders the questions panel and waiting banner when the job is paused', async () => {
      await setup();
      showActiveJob({ waiting_for_answers: true, pending_questions: [QUESTION] });
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.github-banner--waiting')).not.toBeNull();
      expect(el.querySelector('app-pending-questions')).not.toBeNull();
      expect(el.textContent).toContain('Which auth flow?');
    });

    it('hides the panel when the job is not waiting for answers', async () => {
      await setup();
      showActiveJob({ waiting_for_answers: false, pending_questions: [QUESTION] });
      expect(fixture.nativeElement.querySelector('app-pending-questions')).toBeNull();
    });

    it('hides the panel when there are no pending questions', async () => {
      await setup();
      showActiveJob({ waiting_for_answers: true, pending_questions: [] });
      expect(fixture.nativeElement.querySelector('app-pending-questions')).toBeNull();
      expect(component.hasPendingQuestions()).toBe(false);
    });

    it('shows the waiting badge in the job panel header while paused', async () => {
      await setup();
      showActiveJob({ waiting_for_answers: true, pending_questions: [QUESTION] });
      const badge = fixture.nativeElement.querySelector('.github-job-status--waiting_for_user');
      expect(badge?.textContent).toContain('waiting for your answers');
    });

    it('onAnswersSubmitted folds the post-submit status in and restarts polling', async () => {
      await setup();
      component.activeJob = { job_id: 'j1', issue_number: 5, issue_url: 'u', status: 'queued', message: '' };
      component['startPolling']('j1');
      const staleSub = component['pollSub'];
      component.issueError = 'Lost connection to the coding team — status polling failed.';

      const resumed = { job_id: 'j1', status: 'running', waiting_for_answers: false };
      component.onAnswersSubmitted(resumed as any);

      expect(component.jobStatus).toEqual(resumed);
      // Polling restarted from scratch: stale in-flight fetches are discarded.
      expect(staleSub?.closed).toBe(true);
      expect(component['pollSub']).not.toBeNull();
      expect(component['pollSub']).not.toBe(staleSub);
      // A stale "lost connection" banner is cleared — answering proves the link is back.
      expect(component.issueError).toBeNull();
      component['stopPolling']();
    });
  });

  // -------------------------------------------------------------------------
  // Active-job restore across reloads
  // -------------------------------------------------------------------------

  describe('active job restore', () => {
    const GH_JOB = {
      job_id: 'j-restore',
      status: 'waiting_for_user',
      phase: 'paused',
      updated_at: '2026-06-09T10:00:00Z',
      waiting_for_answers: true,
      github_context: { owner: 'acme', repo: 'widgets', issue_number: 2, issue_url: 'https://example.com/2' },
    };

    it('re-attaches to a non-terminal GitHub-issue job on init', async () => {
      apiSpy.listJobs.mockReturnValue(of([GH_JOB]));
      await setup();
      // Must request active-only so terminal jobs' full records never cross the wire.
      expect(apiSpy.listJobs).toHaveBeenCalledWith(true);
      expect(component.activeJob?.job_id).toBe('j-restore');
      expect(component.activeJob?.issue_number).toBe(2);
      expect(component.activeJob?.issue_url).toBe('https://example.com/2');
      // The poller's first fetch is immediate (timer(0)), so the restored
      // panel is hydrated without waiting a full poll interval.
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(apiSpy.getJobStatus).toHaveBeenCalledWith('j-restore');
      expect(component.activeIssueNumbers.has(2)).toBe(true);
      component['stopPolling']();
    });

    it('ignores jobs belonging to a different repository', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          { ...GH_JOB, github_context: { ...GH_JOB.github_context, repo: 'other-repo' } },
          { ...GH_JOB, job_id: 'other-owner', github_context: { ...GH_JOB.github_context, owner: 'someone-else' } },
        ]),
      );
      await setup();
      expect(component.activeJob).toBeNull();
      expect(component.activeIssueNumbers.size).toBe(0);
    });

    it('does not restore at all when GitHub is not configured', async () => {
      integrationsSpy.getGitHubConfig.mockReturnValue(of({ ...CONFIGURED, token_configured: false }));
      apiSpy.listJobs.mockReturnValue(of([GH_JOB]));
      await setup();
      expect(apiSpy.listJobs).not.toHaveBeenCalled();
      expect(component.activeJob).toBeNull();
    });

    it('does not adopt a job when the list resolves after destroy', async () => {
      const jobs$ = new Subject<CodingTeamJobListItem[]>();
      apiSpy.listJobs.mockReturnValue(jobs$.asObservable());
      await setup();
      fixture.destroy();
      jobs$.next([GH_JOB]);
      expect(component.activeJob).toBeNull();
      expect(component['pollSub']).toBeNull();
    });

    it('ignores terminal jobs and jobs without a GitHub issue', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          { ...GH_JOB, job_id: 'done', status: 'completed' },
          { job_id: 'local-run', status: 'running', repo_path: '/tmp/x' },
        ]),
      );
      await setup();
      expect(component.activeJob).toBeNull();
      expect(component.activeIssueNumbers.size).toBe(0);
    });

    it('picks the most recently updated job when several are active', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          { ...GH_JOB, job_id: 'older', updated_at: '2026-06-08T10:00:00Z' },
          {
            ...GH_JOB,
            job_id: 'newer',
            updated_at: '2026-06-09T11:00:00Z',
            github_context: { ...GH_JOB.github_context, issue_number: 3 },
          },
        ]),
      );
      await setup();
      expect(component.activeJob?.job_id).toBe('newer');
      // Both issues are still flagged as in progress.
      expect(component.activeIssueNumbers).toEqual(new Set([2, 3]));
      component['stopPolling']();
    });

    it('does not replace a job the user just started', async () => {
      await setup();
      component.activeJob = { job_id: 'mine', issue_number: 9, issue_url: '', status: 'queued', message: '' };
      apiSpy.listJobs.mockReturnValue(of([GH_JOB]));
      component['restoreActiveJob']();
      expect(component.activeJob.job_id).toBe('mine');
      expect(component.activeIssueNumbers.has(2)).toBe(true);
    });

    it('stays usable when the job list cannot be fetched', async () => {
      apiSpy.listJobs.mockReturnValue(throwError(() => new Error('down')));
      await setup();
      expect(component.activeJob).toBeNull();
      expect(component.githubConfigured).toBe(true);
    });

    it('renders an "In progress" chip on issues with an active job', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([{ ...GH_JOB, github_context: { ...GH_JOB.github_context, issue_number: 2 } }]),
      );
      await setup();
      // Dismiss the restored job so the issue list is visible again.
      component.dismissJob();
      fixture.detectChanges();
      const chips = fixture.nativeElement.querySelectorAll('.github-label-chip--active');
      expect(chips.length).toBe(1);
      expect(chips[0].textContent).toContain('In progress');
      component['stopPolling']();
    });

    it('marks a just-started issue as in progress', async () => {
      await setup();
      const issue = component.issues[0];
      integrationsSpy.runGitHubIssue.mockReturnValue(
        of({ job_id: 'j1', issue_number: issue.number, issue_url: 'u', status: 'queued', message: '' }),
      );
      component.selectIssue(issue);
      component.confirmAndRun();
      expect(component.isIssueInProgress(issue)).toBe(true);
      component['stopPolling']();
    });

    it('removes the in-progress chip when the poller observes a terminal status', async () => {
      await setup();
      component.activeJob = { job_id: 'j1', issue_number: 7, issue_url: 'u', status: 'queued', message: '' };
      component.activeIssueNumbers.add(7);
      apiSpy.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'completed' }));
      component['startPolling']('j1');
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(component.activeIssueNumbers.has(7)).toBe(false);
    });

    it('dismissing a finished job removes its chip; dismissing a running one keeps it', async () => {
      await setup();
      // Running case: the server still lists the job after dismiss, so the chip survives
      // — but the dismissed job itself is never re-adopted into the panel.
      apiSpy.listJobs.mockReturnValue(
        of([
          {
            job_id: 'j1',
            status: 'running',
            github_context: { owner: 'acme', repo: 'widgets', issue_number: 7 },
          },
        ]),
      );
      component.activeJob = { job_id: 'j1', issue_number: 7, issue_url: 'u', status: 'queued', message: '' };
      component.activeIssueNumbers.add(7);
      component.jobStatus = { job_id: 'j1', status: 'running' };
      component.dismissJob();
      expect(component.activeIssueNumbers.has(7)).toBe(true);
      expect(component.activeJob).toBeNull();

      // Finished case: the active-jobs list no longer contains it → chip removed.
      apiSpy.listJobs.mockReturnValue(of([]));
      component.activeJob = { job_id: 'j2', issue_number: 7, issue_url: 'u', status: 'queued', message: '' };
      component.jobStatus = { job_id: 'j2', status: 'completed' };
      component.dismissJob();
      expect(component.activeIssueNumbers.has(7)).toBe(false);
      expect(component.activeJob).toBeNull();
    });

    it('dismissing one job adopts another active, non-dismissed job', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          { ...GH_JOB, job_id: 'j-a', updated_at: '2026-06-09T12:00:00Z' },
          {
            ...GH_JOB,
            job_id: 'j-b',
            updated_at: '2026-06-09T11:00:00Z',
            github_context: { ...GH_JOB.github_context, issue_number: 3 },
          },
        ]),
      );
      await setup();
      expect(component.activeJob?.job_id).toBe('j-a');

      component.jobStatus = { job_id: 'j-a', status: 'running' };
      component.dismissJob();
      // The dismissed job is excluded; the other in-flight job takes the panel.
      expect(component.activeJob?.job_id).toBe('j-b');
      expect(component.activeJob?.issue_number).toBe(3);
      component['stopPolling']();
    });

    it('matches the configured repository case-insensitively', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([{ ...GH_JOB, github_context: { ...GH_JOB.github_context, owner: 'Acme', repo: 'Widgets' } }]),
      );
      await setup();
      expect(component.activeJob?.job_id).toBe('j-restore');
      expect(component.activeIssueNumbers.has(2)).toBe(true);
      component['stopPolling']();
    });

    it('a stale jobs snapshot cannot wipe the chip of a just-started run', async () => {
      await setup();
      component.activeJob = { job_id: 'mine', issue_number: 9, issue_url: '', status: 'queued', message: '' };
      component.jobStatus = { job_id: 'mine', status: 'running' };
      apiSpy.listJobs.mockReturnValue(of([]));
      component['restoreActiveJob']();
      expect(component.activeIssueNumbers.has(9)).toBe(true);
    });

    it('does not re-add the chip for a displayed job that has finished', async () => {
      await setup();
      // The poller dropped #9 when the job went terminal; a refresh must not resurrect it.
      component.activeJob = { job_id: 'mine', issue_number: 9, issue_url: '', status: 'queued', message: '' };
      component.jobStatus = { job_id: 'mine', status: 'completed' };
      apiSpy.listJobs.mockReturnValue(of([]));
      component['restoreActiveJob']();
      expect(component.activeIssueNumbers.has(9)).toBe(false);
    });

    it('renders the Dismiss button even while the job is non-terminal', async () => {
      await setup();
      component.activeJob = { job_id: 'j1', issue_number: 7, issue_url: 'u', status: 'queued', message: '' };
      component.jobStatus = { job_id: 'j1', status: 'waiting_for_user' };
      fixture.detectChanges();
      const header = fixture.nativeElement.querySelector('.github-job-panel__header');
      expect(header?.textContent).toContain('Dismiss');
    });
  });
});
