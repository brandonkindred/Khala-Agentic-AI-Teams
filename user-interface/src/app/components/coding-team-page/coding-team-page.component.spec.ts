import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
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
});
