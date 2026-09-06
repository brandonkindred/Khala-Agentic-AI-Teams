import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Subject, of, throwError } from 'rxjs';
import type { CodingTeamJobListItem, CodingTeamJobStatus } from '../../models/coding-team.model';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { vi } from 'vitest';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { NotificationService } from '../../core/notification.service';
import { CodingTeamPageComponent } from './coding-team-page.component';
import type {
  GitHubConfigResponse,
  GitHubIssueItem,
  GitHubPullRequestItem,
  GitHubRepoItem,
} from '../../models/integrations.model';

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

/** One open pull request for the PR-tab tests. */
function ghPull(number: number, overrides: Partial<GitHubPullRequestItem> = {}): GitHubPullRequestItem {
  return {
    number,
    title: `PR ${number}`,
    body_preview: 'body',
    author: 'octocat',
    html_url: `https://github.com/acme/widgets/pull/${number}`,
    head: `feature-${number}`,
    base: 'main',
    draft: false,
    labels: [],
    updated_at: '2026-06-09T10:00:00Z',
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
    // toggleRepo() now writes to real localStorage (last-used-repo memory); start every test with
    // a clean slate so one test's expand can't leak into another's "fresh visit" assertions.
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
      getGitHubPullRequests: vi.fn().mockReturnValue(of([])),
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
    // The template iterates the precomputed view-models; rebuild them from the run list via the
    // same private builder the component uses (bracket access, the spec's idiom for internals —
    // no structural cast to a private method's shape).
    component['buildRunVms']();
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

  describe('loading states render via app-loading-spinner (role="status")', () => {
    /** Every `[role="status"]` element currently rendered, for locating the one with the expected text. */
    function statusRegions(): HTMLElement[] {
      return Array.from(fixture.nativeElement.querySelectorAll('[role="status"]'));
    }

    it('shows "Checking GitHub integration…" via app-loading-spinner while isLoadingConfig is true', async () => {
      const configSubject = new Subject<GitHubConfigResponse>();
      integrationsSpy.getGitHubConfig.mockReturnValue(configSubject.asObservable());
      await setup();
      showView('github');
      expect(component.isLoadingConfig).toBe(true);
      expect(fixture.nativeElement.querySelector('app-loading-spinner')).not.toBeNull();
      expect(fixture.nativeElement.querySelector('.github-section--loading')).toBeNull();
      expect(statusRegions().some((el) => el.textContent?.includes('Checking GitHub integration…'))).toBe(true);
      configSubject.next(CONFIGURED);
      configSubject.complete();
      fixture.detectChanges();
      expect(component.isLoadingConfig).toBe(false);
      expect(fixture.nativeElement.querySelector('app-loading-spinner')).toBeNull();
    });

    it('shows "Loading repositories…" via app-loading-spinner while loadingRepos is true', async () => {
      const reposSubject = new Subject<GitHubRepoItem[]>();
      integrationsSpy.getGitHubRepos.mockReturnValue(reposSubject.asObservable());
      await setup();
      showView('github');
      expect(component.loadingRepos).toBe(true);
      expect(fixture.nativeElement.querySelector('app-loading-spinner')).not.toBeNull();
      expect(statusRegions().some((el) => el.textContent?.includes('Loading repositories…'))).toBe(true);
      reposSubject.next([REPO]);
      reposSubject.complete();
      fixture.detectChanges();
      expect(component.loadingRepos).toBe(false);
      expect(fixture.nativeElement.querySelector('app-loading-spinner')).toBeNull();
    });

    it('shows "Loading issues…" via app-loading-spinner while loadingIssues is true', async () => {
      const issuesSubject = new Subject<GitHubIssueItem[]>();
      integrationsSpy.getGitHubIssues.mockReturnValue(issuesSubject.asObservable());
      await setup();
      showView('github');
      expandFirstRepo();
      expect(component.loadingIssues).toBe(true);
      expect(fixture.nativeElement.querySelector('app-loading-spinner')).not.toBeNull();
      expect(statusRegions().some((el) => el.textContent?.includes('Loading issues…'))).toBe(true);
      issuesSubject.next(makeIssues(1));
      issuesSubject.complete();
      fixture.detectChanges();
      expect(component.loadingIssues).toBe(false);
      expect(fixture.nativeElement.querySelector('app-loading-spinner')).toBeNull();
    });

    it('shows "Starting…" via app-loading-spinner for a selected run with no status yet', async () => {
      await setup();
      const run = ghRun({ status: 'running' });
      component.runs = [run];
      component.runningRuns = [run];
      component.recentRuns = [];
      // The template iterates the precomputed view-models, so they must be rebuilt from the run
      // list via the same private builder the component uses (bracket access, the spec's idiom
      // for internals — no structural cast to a private method's shape) before toggleRun renders it.
      component['buildRunVms']();
      // toggleRun selects the run and (re)starts polling on a timer, so `jobStatus` stays null
      // until the poller's first (async) tick — the window this "Starting…" branch covers.
      component.toggleRun(run);
      fixture.detectChanges();
      expect(component.jobStatus).toBeNull();
      expect(fixture.nativeElement.querySelector('app-loading-spinner')).not.toBeNull();
      expect(statusRegions().some((el) => el.textContent?.includes('Starting…'))).toBe(true);
      // Let the poller's first (async) tick land, matching the teardown check the three sibling tests make.
      await flushAsync();
      fixture.detectChanges();
      expect(component.jobStatus).not.toBeNull();
      expect(fixture.nativeElement.querySelector('app-loading-spinner')).toBeNull();
    });
  });

  describe('empty states render in a role="status" region (no repo access / no open issues)', () => {
    it('renders "no repository access" in a role="status" region with its icon hidden from AT', async () => {
      integrationsSpy.getGitHubRepos.mockReturnValue(of([]));
      await setup();
      showView('github');
      const empty = fixture.nativeElement.querySelector('.github-empty');
      expect(empty).not.toBeNull();
      expect(empty?.getAttribute('role')).toBe('status');
      expect(empty?.textContent).toContain('The personal access token has no repository access.');
      expect(empty?.querySelector('mat-icon')?.getAttribute('aria-hidden')).toBe('true');
    });

    it('renders "no open issues" in a role="status" region with its icon hidden from AT', async () => {
      integrationsSpy.getGitHubIssues.mockReturnValue(of([]));
      await setup();
      showView('github');
      expandFirstRepo();
      const empty = fixture.nativeElement.querySelector('.github-empty');
      expect(empty).not.toBeNull();
      expect(empty?.getAttribute('role')).toBe('status');
      expect(empty?.textContent).toContain('No open issues found.');
      expect(empty?.querySelector('mat-icon')?.getAttribute('aria-hidden')).toBe('true');
    });
  });

  it('auto-loads the accessible repositories on init when GitHub is configured', async () => {
    await setup();
    expect(integrationsSpy.getGitHubConfig).toHaveBeenCalled();
    expect(integrationsSpy.getGitHubRepos).toHaveBeenCalled();
    expect(component.githubConfigured).toBe(true);
    expect(component.reposLoaded).toBe(true);
    expect(component.repos.length).toBe(1);
    // Issues are per-repo: nothing loads until a repo is expanded.
    expect(integrationsSpy.getGitHubIssues).not.toHaveBeenCalled();
  });

  it('renders the repo-list error banner in the GitHub view', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(
      throwError(() => ({ error: { detail: 'bad credentials' } })),
    );
    await setup();
    showView('github');
    const banner = fixture.nativeElement.querySelector('app-inline-banner[variant="error"]');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('bad credentials');
  });

  it('loads the expanded repo\'s issues, scoped by owner/repo and the configured default label', async () => {
    await setup();
    showView('github');
    expandFirstRepo();
    // The CONFIGURED mock carries default_label 'ai', applied as a global filter.
    expect(integrationsSpy.getGitHubIssues).toHaveBeenCalledWith({ owner: 'acme', repo: 'widgets', label: 'ai' });
    expect(component.issuesLoaded).toBe(true);
    expect(component.issues.length).toBe(3);
    // The active filter is surfaced so an empty repo is explained, not silent.
    const filter = fixture.nativeElement.querySelector('.github-label-filter');
    expect(filter?.textContent).toContain('ai');
  });

  it('passes label undefined and shows no filter chip when no default label is configured', async () => {
    integrationsSpy.getGitHubConfig.mockReturnValue(
      of({ enabled: true, token_configured: true, default_label: '' }),
    );
    await setup();
    showView('github');
    expandFirstRepo();
    // The component passes label: undefined; that undefined does not become a `label` query
    // param is asserted at the HTTP boundary in integrations-api.service.spec.ts.
    const arg = integrationsSpy.getGitHubIssues.mock.calls.at(-1)?.[0];
    expect(arg).toMatchObject({ owner: 'acme', repo: 'widgets' });
    expect(arg?.label).toBeUndefined();
    expect(fixture.nativeElement.querySelector('.github-label-filter')).toBeNull();
  });

  it('toggling the default-label filter off reloads issues unfiltered (and back on re-applies it)', async () => {
    await setup(); // CONFIGURED carries default_label 'ai'
    showView('github');
    expandFirstRepo();
    expect(component.activeLabel()).toBe('ai');
    integrationsSpy.getGitHubIssues.mockClear();

    component.toggleLabelFilter(); // turn the filter off
    fixture.detectChanges();
    expect(component.labelFilterActive).toBe(false);
    expect(component.activeLabel()).toBeUndefined();
    expect(integrationsSpy.getGitHubIssues.mock.calls.at(-1)?.[0]?.label).toBeUndefined();
    expect(fixture.nativeElement.querySelector('.github-label-filter')?.textContent).toContain('all issues');

    component.toggleLabelFilter(); // turn it back on
    fixture.detectChanges();
    expect(component.activeLabel()).toBe('ai');
    expect(integrationsSpy.getGitHubIssues.mock.calls.at(-1)?.[0]?.label).toBe('ai');
  });

  it('the label-filter toggle button is wired to toggleLabelFilter (template click)', async () => {
    await setup(); // default_label 'ai'
    showView('github');
    expandFirstRepo();
    const btn: HTMLButtonElement | null = fixture.nativeElement.querySelector('.github-label-filter__toggle');
    expect(btn).toBeTruthy();
    btn!.click();
    fixture.detectChanges();
    expect(component.labelFilterActive).toBe(false);
  });

  it('the label toggle reloads the issue list only (no Runs refetch)', async () => {
    await setup();
    showView('github');
    expandFirstRepo();
    const runsCallsBefore = apiSpy.listJobs.mock.calls.length;
    component.toggleLabelFilter();
    // Toggling the filter skips the refreshTrigger$ that refetches the Runs list.
    expect(apiSpy.listJobs.mock.calls.length).toBe(runsCallsBefore);
  });

  it('resets the label filter to on when a different repo is expanded (per-repo, not global)', async () => {
    await setup(); // default_label 'ai'
    showView('github');
    expandFirstRepo(); // repo A, filter on
    component.toggleLabelFilter(); // turn it off for repo A
    expect(component.labelFilterActive).toBe(false);
    // Expand a different repo.
    const other: GitHubRepoItem = { ...REPO, full_name: 'other/thing', owner: 'other', name: 'thing' };
    component.repos = [REPO, other];
    component.toggleRepo(other);
    // The newly-expanded repo starts with the operator's configured filter applied again.
    expect(component.labelFilterActive).toBe(true);
    expect(component.activeLabel()).toBe('ai');
  });

  it('collapsing the expanded repo clears its issue state', async () => {
    await setup();
    expandFirstRepo();
    expect(component.selectedRepo?.full_name).toBe('acme/widgets');
    component.toggleRepo(component.repos[0]);
    expect(component.selectedRepo).toBeNull();
    expect(component.issuesLoaded).toBe(false);
    expect(component.issues.length).toBe(0);
  });

  it('surfaces an error when loading repositories fails', async () => {
    integrationsSpy.getGitHubRepos.mockReturnValue(
      throwError(() => ({ error: { detail: 'bad credentials' } })),
    );
    await setup();
    expect(component.repoError).toBe('bad credentials');
    expect(component.loadingRepos).toBe(false);
  });

  it('polls the Runs list with terminal jobs included (active=false)', async () => {
    await setup();
    await flushAsync();
    expect(apiSpy.listJobs).toHaveBeenCalledWith(false);
  });

  it('does NOT auto-load repos or poll runs when GitHub is not configured', async () => {
    integrationsSpy.getGitHubConfig.mockReturnValue(
      of({ ...CONFIGURED, token_configured: false }),
    );
    await setup();
    await flushAsync();
    expect(component.githubConfigured).toBe(false);
    expect(integrationsSpy.getGitHubRepos).not.toHaveBeenCalled();
    expect(apiSpy.listJobs).not.toHaveBeenCalled();
  });

  it('handles a failed config check without loading repos', async () => {
    integrationsSpy.getGitHubConfig.mockReturnValue(throwError(() => new Error('boom')));
    await setup();
    expect(component.githubConfigured).toBe(false);
    expect(component.isLoadingConfig).toBe(false);
    expect(integrationsSpy.getGitHubRepos).not.toHaveBeenCalled();
  });

  it('paginates client-side and resets to the first page on (re)load', async () => {
    integrationsSpy.getGitHubIssues.mockReturnValue(of(makeIssues(25)));
    await setup();
    expandFirstRepo();

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
    expandFirstRepo();
    expect(component.issueError).toBe('rate limited');
    expect(component.loadingIssues).toBe(false);
  });

  it('selects an issue, cancels, then runs it — selecting the new run, no dismiss', async () => {
    await setup();
    expandFirstRepo();
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
    // The expanded repo is the run target — repository access comes from the PAT.
    expect(integrationsSpy.runGitHubIssue).toHaveBeenCalledWith({
      issue_number: issue.number,
      owner: 'acme',
      repo: 'widgets',
    });
    expect(component.selectedRunId).toBe('j1');
    expect(component.selectedRunNumber).toBe(issue.number);
    expect(component.selectedRunOwner).toBe('acme');
    expect(component.selectedRunRepo).toBe('widgets');
    expect(component.selectedIssue).toBeNull();
    expect(component.isIssueInProgress(issue)).toBe(true);
    // The panel is not dismissable.
    expect('dismissJob' in component).toBe(false);
  });

  it('surfaces an error when starting a run fails', async () => {
    await setup();
    expandFirstRepo();
    const issue = component.issues[0];
    integrationsSpy.runGitHubIssue.mockReturnValue(throwError(() => ({ error: { detail: 'duplicate run' } })));
    component.selectIssue(issue);
    component.confirmAndRun();
    expect(component.issueError).toBe('duplicate run');
    expect(component.runningIssue).toBe(false);
  });

  it('does not attribute a run-start failure to the repo the user switched to', async () => {
    await setup();
    expandFirstRepo(); // acme/widgets expanded
    const issue = component.issues[0];
    const slow = new Subject<never>();
    integrationsSpy.runGitHubIssue.mockReturnValue(slow.asObservable());
    component.selectIssue(issue);
    component.confirmAndRun(); // targets acme/widgets (request pending)
    // The user switches to another repo while the run-start is on the wire.
    component.selectedRepo = { ...REPO, full_name: 'other/thing', owner: 'other', name: 'thing' };
    slow.error({ error: { detail: 'duplicate run' } });
    // issueError is the expanded repo's banner — acme/widgets' failure must not show under other/thing.
    expect(component.issueError).toBeNull();
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

  it('confirmAndRun is a no-op while a run is already starting (guards double-submit)', async () => {
    await setup();
    expandFirstRepo();
    component.selectIssue(component.issues[0]);
    component.runningIssue = true;
    component.confirmAndRun();
    expect(integrationsSpy.runGitHubIssue).not.toHaveBeenCalled();
  });

  it('loadIssues is a no-op when no repo is expanded', async () => {
    await setup();
    integrationsSpy.getGitHubIssues.mockClear();
    component.selectedRepo = null;
    component.loadIssues();
    expect(integrationsSpy.getGitHubIssues).not.toHaveBeenCalled();
  });

  it('discards an issue response that lands after the user switched repos', async () => {
    await setup();
    const slow = new Subject<GitHubIssueItem[]>();
    integrationsSpy.getGitHubIssues.mockReturnValue(slow.asObservable());
    component.selectedRepo = REPO;
    component.loadIssues();
    // The user switches to another repo while the acme/widgets request is on the wire.
    component.selectedRepo = { ...REPO, full_name: 'other/thing', owner: 'other', name: 'thing' };
    slow.next(makeIssues(2));
    slow.complete();
    // The stale response is dropped — it must never render under the other repo's row.
    expect(component.issues.length).toBe(0);
    expect(component.issuesLoaded).toBe(false);
  });

  it('confirms a launched workflow with a transient snackbar', async () => {
    await setup();
    component.onWorkflowLaunched({ job_id: 'wf-1', conversation_id: 'c1' });
    expect(notificationsSpy.saved).toHaveBeenCalledWith('Coding job queued — id wf-1.');
  });

  it('does not confirm when no job id is returned', async () => {
    await setup();
    component.onWorkflowLaunched({ job_id: null, conversation_id: 'c1' });
    expect(notificationsSpy.saved).not.toHaveBeenCalled();
  });

  // -------------------------------------------------------------------------
  // View switcher (Chat / GitHub / Jobs)
  // -------------------------------------------------------------------------

  describe('view switcher', () => {
    it('opens on the Jobs view showing the Runs panel, not the Chat or GitHub views', async () => {
      await setup();
      expect(component.activeView).toBe('jobs');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.jobs-panel')).not.toBeNull();
      expect(el.querySelector('app-team-assistant-chat')).toBeNull();
      expect(el.querySelector('.github-section')).toBeNull();
    });

    it('shows only the GitHub repos panel when the GitHub view is active', async () => {
      await setup();
      showView('github');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelectorAll('.github-repo-row').length).toBe(1);
      expect(el.querySelector('app-team-assistant-chat')).toBeNull();
      expect(el.querySelector('.jobs-panel')).toBeNull();
    });

    it('expanding a repo row reveals its issues inline', async () => {
      await setup();
      showView('github');
      expandFirstRepo();
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelectorAll('.github-issue-row').length).toBe(3);
      // The repo row shows GitHub's open-items hint.
      expect(el.querySelector('.github-repo-row__issues')?.textContent).toContain('3 open');
    });

    it('shows only the Jobs panel when the Jobs view is active', async () => {
      await setup();
      showView('jobs');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.jobs-panel')).not.toBeNull();
      expect(el.querySelector('app-team-assistant-chat')).toBeNull();
      expect(el.querySelector('.github-repo-row')).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // Primary-action discoverability
  // -------------------------------------------------------------------------

  describe('primary-action discoverability', () => {
    it('signposts the repo → issue → confirm flow before any repo is expanded', async () => {
      await setup();
      showView('github');
      const el: HTMLElement = fixture.nativeElement;
      const hint = el.querySelector('.github-flow-hint');
      expect(hint).not.toBeNull();
      expect(hint?.textContent).toContain('Select a repo, then an issue');
    });

    it('hides the flow hint once a repo is expanded', async () => {
      await setup();
      showView('github');
      expandFirstRepo();
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.github-flow-hint')).toBeNull();
    });

    it('renders the Confirm & Start action bar when an issue is selected', async () => {
      await setup();
      expandFirstRepo();
      component.selectIssue(component.issues[0]);
      showView('github');
      const el: HTMLElement = fixture.nativeElement;
      const actions = el.querySelector('.github-confirm-panel__actions');
      expect(actions).not.toBeNull();
      const confirmBtn = actions?.querySelector('button') as HTMLButtonElement;
      expect(confirmBtn).not.toBeNull();
      expect(confirmBtn.textContent).toContain('Confirm');
      expect(confirmBtn.disabled).toBe(false);
    });
  });

  // -------------------------------------------------------------------------
  // Repo search
  // -------------------------------------------------------------------------

  describe('repo search', () => {
    const REPO_B: GitHubRepoItem = { ...REPO, name: 'gadgets', full_name: 'acme/gadgets' };
    const REPO_C: GitHubRepoItem = { ...REPO, owner: 'other', name: 'widgets', full_name: 'other/widgets' };

    beforeEach(() => {
      integrationsSpy.getGitHubRepos.mockReturnValue(of([REPO, REPO_B, REPO_C]));
    });

    it('narrows the visible repo rows as the user types', async () => {
      await setup();
      showView('github');
      component.repoSearch = 'gadgets';
      fixture.detectChanges();
      const el: HTMLElement = fixture.nativeElement;
      const rows = Array.from(el.querySelectorAll('.github-repo-row'));
      expect(rows.length).toBe(1);
      expect(rows[0].textContent).toContain('acme/gadgets');
    });

    it('matches full_name case-insensitively as a substring', async () => {
      await setup();
      showView('github');
      component.repoSearch = 'OTHER/WID';
      fixture.detectChanges();
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelectorAll('.github-repo-row').length).toBe(1);
    });

    it('clearing the search restores the full repo list', async () => {
      await setup();
      showView('github');
      component.repoSearch = 'gadgets';
      fixture.detectChanges();
      component.clearRepoSearch();
      fixture.detectChanges();
      const el: HTMLElement = fixture.nativeElement;
      expect(component.repoSearch).toBe('');
      expect(el.querySelectorAll('.github-repo-row').length).toBe(3);
    });

    it('shows a no-matches empty state with a Clear search action when nothing matches', async () => {
      await setup();
      showView('github');
      component.repoSearch = 'nonexistent-repo-xyz';
      fixture.detectChanges();
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelectorAll('.github-repo-row').length).toBe(0);
      expect(el.textContent).toContain('No repositories match');
      const clearBtn = Array.from(el.querySelectorAll('button')).find((b) => b.textContent?.includes('Clear search'));
      expect(clearBtn).toBeTruthy();
    });

    // The repo search field is the only one in the DOM while no repo is expanded, so this
    // selector is unambiguous here; the issue-search announcer is nested in .github-repo-issues.
    function repoAnnouncer(): string {
      const el: HTMLElement = fixture.nativeElement;
      return el.querySelector('.github-search-field p[role="status"]')?.textContent?.trim() ?? '';
    }

    it('announces the filtered repo count in a polite live region as the search narrows the list', async () => {
      await setup();
      showView('github');
      expect(repoAnnouncer()).toBe('3 repositories shown');
      component.repoSearch = 'gadgets';
      fixture.detectChanges();
      expect(repoAnnouncer()).toBe('1 repository shown');
      component.repoSearch = 'nonexistent-repo-xyz';
      fixture.detectChanges();
      expect(repoAnnouncer()).toBe('0 repositories shown');
    });

    it('announces nothing when the token has no repository access (the search field is not rendered)', async () => {
      integrationsSpy.getGitHubRepos.mockReturnValue(of([]));
      await setup();
      showView('github');
      expect(component.repoCountAnnouncement).toBe('');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.github-search-field p[role="status"]')).toBeNull();
    });

    it('preserves aria-expanded/aria-controls semantics for a row that survives a search, even when its list index shifts', async () => {
      await setup();
      showView('github');
      component.toggleRepo(REPO_C); // expands "other/widgets", originally at index 2
      component.repoSearch = 'widgets'; // matches acme/widgets (idx 0) and other/widgets (now idx 1)
      fixture.detectChanges();
      const el: HTMLElement = fixture.nativeElement;
      const rows = Array.from(el.querySelectorAll('.github-repo-row')) as HTMLElement[];
      expect(rows.length).toBe(2);
      const expandedRow = rows.find((r) => r.getAttribute('aria-expanded') === 'true');
      expect(expandedRow?.textContent).toContain('other/widgets');
      const controlsId = expandedRow?.getAttribute('aria-controls');
      expect(controlsId).toBe('repo-issues-1');
      const panel = el.querySelector(`#${controlsId}`);
      expect(panel).not.toBeNull();
      expect(panel?.classList.contains('github-repo-issues')).toBe(true);
      const collapsedRow = rows.find((r) => r !== expandedRow);
      expect(collapsedRow?.getAttribute('aria-expanded')).toBe('false');
    });
  });

  // -------------------------------------------------------------------------
  // Issue search
  // -------------------------------------------------------------------------

  describe('issue search', () => {
    /** Mirrors what the template's (ngModelChange) triggers on a real keystroke. */
    function setIssueSearch(value: string): void {
      component.issueSearch = value;
      component.onIssueSearchChange();
      fixture.detectChanges();
    }

    it('narrows the visible issue rows as the user types', async () => {
      await setup();
      showView('github');
      expandFirstRepo();
      setIssueSearch('Issue 2');
      const el: HTMLElement = fixture.nativeElement;
      const rows = Array.from(el.querySelectorAll('.github-issue-row'));
      expect(rows.length).toBe(1);
      expect(rows[0].textContent).toContain('Issue 2');
    });

    it('clearing the search restores the full issue list', async () => {
      await setup();
      showView('github');
      expandFirstRepo();
      setIssueSearch('Issue 2');
      component.clearIssueSearch();
      fixture.detectChanges();
      const el: HTMLElement = fixture.nativeElement;
      expect(component.issueSearch).toBe('');
      expect(el.querySelectorAll('.github-issue-row').length).toBe(3);
    });

    it('shows a no-matches empty state with a Clear search action when nothing matches', async () => {
      await setup();
      showView('github');
      expandFirstRepo();
      setIssueSearch('nonexistent-issue-xyz');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelectorAll('.github-issue-row').length).toBe(0);
      expect(el.textContent).toContain('No issues match');
      const clearBtn = Array.from(el.querySelectorAll('button')).find((b) => b.textContent?.includes('Clear search'));
      expect(clearBtn).toBeTruthy();
    });

    // Scoped to the expanded repo's panel: the repo-search announcer is also in the DOM here.
    function issueAnnouncer(): string {
      const el: HTMLElement = fixture.nativeElement;
      return (
        el.querySelector('.github-repo-issues .github-search-field p[role="status"]')?.textContent?.trim() ?? ''
      );
    }

    it('announces the filtered issue count in a polite live region as the search narrows the list', async () => {
      await setup();
      showView('github');
      expandFirstRepo();
      expect(issueAnnouncer()).toBe('3 issues shown');
      setIssueSearch('Issue 2');
      expect(issueAnnouncer()).toBe('1 issue shown');
      setIssueSearch('nonexistent-issue-xyz');
      expect(issueAnnouncer()).toBe('0 issues shown');
    });

    it('announces nothing when the repo has no open issues (the search field is not rendered)', async () => {
      integrationsSpy.getGitHubIssues.mockReturnValue(of([]));
      await setup();
      showView('github');
      expandFirstRepo();
      expect(component.issueCountAnnouncement).toBe('');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.github-repo-issues .github-search-field p[role="status"]')).toBeNull();
    });

    it('resets to the first page when a search narrows the list below the current page', async () => {
      integrationsSpy.getGitHubIssues.mockReturnValue(of(makeIssues(25)));
      await setup();
      showView('github');
      expandFirstRepo();
      component.onPageChange({ pageIndex: 1, pageSize: 10, length: 25 });
      fixture.detectChanges();
      expect(component.pagedIssueVms.map((vm) => vm.title)).toContain('Issue 11');

      // "Issue 3" uniquely matches issue #3 — "Issue 13"/"Issue 23" don't contain that substring.
      setIssueSearch('Issue 3');

      expect(component.pageIndex).toBe(0);
      expect(component.pagedIssueVms.length).toBe(1);
      expect(component.pagedIssueVms[0].title).toBe('Issue 3');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelectorAll('.github-issue-row').length).toBe(1);
    });

    it('preserves aria-expanded/aria-controls semantics for an issue row that survives a search', async () => {
      await setup();
      showView('github');
      expandFirstRepo();
      component.selectIssue(component.issues[1]); // "Issue 2"
      fixture.detectChanges();
      setIssueSearch('Issue'); // matches all 3 — the selected row survives unchanged

      const el: HTMLElement = fixture.nativeElement;
      const rows = Array.from(el.querySelectorAll('.github-issue-row')) as HTMLElement[];
      const selectedRow = rows.find((r) => r.getAttribute('aria-expanded') === 'true');
      expect(selectedRow?.textContent).toContain('Issue 2');
      const expectedId = component.confirmPanelId(2);
      expect(selectedRow?.getAttribute('aria-controls')).toBe(expectedId);
      const panel = el.querySelector(`[id="${expectedId}"]`);
      expect(panel).not.toBeNull();
      expect(panel?.classList.contains('github-confirm-panel')).toBe(true);
    });
  });

  // -------------------------------------------------------------------------
  // Last-used repo persistence
  // -------------------------------------------------------------------------

  describe('last-used repo persistence', () => {
    const STORAGE_KEY = 'coding-team-last-repo-v1';

    it('remembers the repo when it is expanded', async () => {
      await setup();
      expandFirstRepo();
      expect(localStorage.getItem(STORAGE_KEY)).toBe('acme/widgets');
    });

    it('does not forget the repo when it is collapsed', async () => {
      await setup();
      expandFirstRepo();
      component.toggleRepo(component.repos[0]); // collapse
      fixture.detectChanges();
      expect(component.selectedRepo).toBeNull();
      expect(localStorage.getItem(STORAGE_KEY)).toBe('acme/widgets');
    });

    it('pre-expands the remembered repo on the next visit', async () => {
      localStorage.setItem(STORAGE_KEY, 'acme/widgets');
      await setup();
      expect(component.selectedRepo?.full_name).toBe('acme/widgets');
      expect(component.issuesLoaded).toBe(true);
      expect(integrationsSpy.getGitHubIssues).toHaveBeenCalledWith(
        expect.objectContaining({ owner: 'acme', repo: 'widgets' }),
      );
    });

    it('does not restore, and surfaces no error, when the remembered repo is no longer accessible', async () => {
      localStorage.setItem(STORAGE_KEY, 'someone-else/gone');
      await setup();
      expect(component.selectedRepo).toBeNull();
      expect(component.repoError).toBeNull();
      expect(component.issueError).toBeNull();
    });

    it('does not restore, and does not throw, when the stored value matches no repo', async () => {
      localStorage.setItem(STORAGE_KEY, '{not json, just garbage}}}');
      await setup();
      expect(component.selectedRepo).toBeNull();
      expect(component.repoError).toBeNull();
    });

    it('does not re-expand a manually-collapsed repo on a later manual refresh', async () => {
      localStorage.setItem(STORAGE_KEY, 'acme/widgets');
      await setup();
      expect(component.selectedRepo?.full_name).toBe('acme/widgets');

      component.toggleRepo(component.repos[0]); // collapse
      fixture.detectChanges();
      expect(component.selectedRepo).toBeNull();

      component.loadRepos(); // manual "Refresh"
      fixture.detectChanges();
      expect(component.selectedRepo).toBeNull();
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
      expect(component.badgeClass('already_complete')).toBe('completed');
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
      // A malformed timestamp resolves to '' rather than rendering "NaNd ago".
      expect(component.timeAgo('not-a-real-date')).toBe('');
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

    it('precomputes run-row view-models (badge/detail/timeAgo) in applyRuns', async () => {
      await setup();
      component['initialRunsLoad'] = false;
      component['applyRuns']([
        ghRun({ job_id: 'a', status: 'running', status_text: 'writing files', updated_at: new Date(Date.now() - 5 * 60000).toISOString() }),
        ghRun({ job_id: 'b', status: 'completed', status_text: 'done', github_context: { owner: 'acme', repo: 'widgets', issue_number: 3 } }),
      ]);
      expect(component.runningRunVms.map((v) => v.run.job_id)).toEqual(['a']);
      const running = component.runningRunVms[0];
      expect(running.badgeClass).toBe('running');
      expect(running.detail).toBe('writing files');
      expect(running.timeAgo).toBe('5m ago');
      // A terminal run's live detail line is suppressed.
      expect(component.recentRunVms.map((v) => v.run.job_id)).toEqual(['b']);
      expect(component.recentRunVms[0].detail).toBeNull();
    });

    it('precomputes issue-row view-models from the visible page and chip set', async () => {
      integrationsSpy.getGitHubIssues.mockReturnValue(
        of([
          issueWith({
            number: 7,
            blocked: true,
            open_dependencies: [3],
            dependencies: [{ number: 3, title: 'A', state: 'open' }],
          }),
        ]),
      );
      await setup();
      expandFirstRepo();
      expect(component.pagedIssueVms.length).toBe(1);
      const vm = component.pagedIssueVms[0];
      expect(vm.number).toBe(7);
      expect(vm.hasDeps).toBe(true);
      expect(vm.blocked).toBe(true);
      expect(vm.openDepsCount).toBe(1);
      expect(vm.depsTooltip).toContain('#3');
      expect(vm.inProgress).toBe(false);
    });

    it('precomputes the selected run\'s badge class and terminal flag when jobStatus is set', async () => {
      await setup();
      component.jobStatus = { job_id: 'j1', status: 'failed' };
      expect(component.jobStatusBadgeClass).toBe('failed');
      expect(component.jobStatusTerminal).toBe(true);
      component.jobStatus = { job_id: 'j1', status: 'running' };
      expect(component.jobStatusBadgeClass).toBe('running');
      expect(component.jobStatusTerminal).toBe(false);
      component.jobStatus = null;
      expect(component.jobStatusBadgeClass).toBe('neutral');
      expect(component.jobStatusTerminal).toBe(false);
    });

    it('announces streaming thinking output, then a settled line count', async () => {
      vi.useFakeTimers();
      try {
        await setup();
        component.jobStatus = { job_id: 'j1', status: 'running' };
        expect(component.thinkingAnnouncement).toBe('');

        // First thinking token: an immediate streaming cue, never the raw stream text.
        component.jobStatus = { job_id: 'j1', status: 'running', thinking: 'Step one' };
        expect(component.thinkingAnnouncement).toBe('Agent is thinking…');

        // A re-render with unchanged thinking text doesn't restart the settle window.
        await vi.advanceTimersByTimeAsync(1000);
        component.jobStatus = { job_id: 'j1', status: 'running', thinking: 'Step one' };
        expect(component.thinkingAnnouncement).toBe('Agent is thinking…');
        await vi.advanceTimersByTimeAsync(500);
        expect(component.thinkingAnnouncement).toBe('1 line of reasoning');

        // New output arriving mid-settle restarts the window instead of announcing early.
        component.jobStatus = { job_id: 'j1', status: 'running', thinking: 'Step one\nStep two' };
        expect(component.thinkingAnnouncement).toBe('Agent is thinking…');
        await vi.advanceTimersByTimeAsync(1000);
        expect(component.thinkingAnnouncement).toBe('Agent is thinking…');
        await vi.advanceTimersByTimeAsync(500);
        expect(component.thinkingAnnouncement).toBe('2 lines of reasoning');
        expect(component.thinkingAnnouncement).not.toContain('Step');
      } finally {
        vi.useRealTimers();
      }
    });

    it('clears the thinking announcement once thinking output is gone', async () => {
      await setup();
      component.jobStatus = { job_id: 'j1', status: 'running', thinking: 'Step one' };
      expect(component.thinkingAnnouncement).toBe('Agent is thinking…');
      component.jobStatus = { job_id: 'j1', status: 'completed' };
      expect(component.thinkingAnnouncement).toBe('');
    });

    it('precomputes the selected issue\'s open-dependency refs when an issue is selected', async () => {
      await setup();
      component.selectIssue(
        issueWith({
          dependencies: [
            { number: 3, title: 'A', state: 'open' },
            { number: 5, title: 'B', state: 'closed' },
          ],
        }),
      );
      expect(component.selectedIssueOpenDepsText).toBe('#3');
      component.cancelSelection();
      expect(component.selectedIssueOpenDepsText).toBe('');
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

  describe('repoRowTooltip', () => {
    it('composes the description and the issue-count clarification when a description is present', async () => {
      await setup();
      const repo: GitHubRepoItem = { ...REPO, description: 'A widget factory.' };
      expect(component.repoRowTooltip(repo)).toBe(
        'A widget factory. — Open issues and pull requests reported by GitHub'
      );
    });

    it('returns just the issue-count clarification when description is null or empty, with no dangling separator', async () => {
      await setup();
      expect(component.repoRowTooltip({ ...REPO, description: null })).toBe(
        'Open issues and pull requests reported by GitHub'
      );
      expect(component.repoRowTooltip({ ...REPO, description: '' })).toBe(
        'Open issues and pull requests reported by GitHub'
      );
      expect(component.repoRowTooltip({ ...REPO, description: '   ' })).toBe(
        'Open issues and pull requests reported by GitHub'
      );
      expect(component.repoRowTooltip({ ...REPO, description: '  Padded widget factory.  ' })).toBe(
        'Padded widget factory. — Open issues and pull requests reported by GitHub'
      );
    });
  });

  describe('issueRowTooltip', () => {
    it('returns the title alone when the issue is not in progress', async () => {
      await setup();
      expect(component.issueRowTooltip({ title: 'Fix the thing', inProgress: false })).toBe('Fix the thing');
    });

    it('appends the in-progress clause when the issue is in progress', async () => {
      await setup();
      expect(component.issueRowTooltip({ title: 'Fix the thing', inProgress: true })).toBe(
        'Fix the thing — The coding team is already working on this issue'
      );
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
      expandFirstRepo();
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
      expandFirstRepo();
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
      expandFirstRepo();
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
      expandFirstRepo();

      component.selectIssue(blocked);
      showView('github');

      const el: HTMLElement = fixture.nativeElement;
      const warning = el.querySelector('app-inline-banner[variant="warning"]');
      expect(warning).not.toBeNull();
      expect(warning?.textContent).toContain('#3');

      const confirmBtn = el.querySelector('.github-confirm-panel__actions button') as HTMLButtonElement;
      expect(confirmBtn.disabled).toBe(false);
    });

    it('keeps the issue list visible and shows the inline confirm under the selected row', async () => {
      await setup();
      expandFirstRepo();
      component.selectIssue(component.issues[1]); // issue #2
      showView('github');
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

    it('appends activity narrative lines when jobStatus gains status_text', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), { job_id: 'j1', status: 'running', status_text: 'Building task graph' });
      expect(component.activityNarrative.lines.map((l) => l.text)).toContain('Status: Building task graph');
      expect(component.thoughtStreamTitle).toBe('Agent activity');
      expect(component.showThoughtStreamPanel).toBe(true);
    });

    it('does not duplicate narrative lines for identical consecutive status snapshots', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), { job_id: 'j1', status: 'running', status_text: 'Building task graph' });
      const n = component.activityNarrative.lines.length;
      component.jobStatus = { job_id: 'j1', status: 'running', status_text: 'Building task graph' };
      expect(component.activityNarrative.lines.length).toBe(n);
    });

    it('clears activity narrative when selecting a different run', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), { job_id: 'j1', status: 'running', status_text: 'Building task graph' });
      expect(component.activityNarrative.lines.length).toBeGreaterThan(0);
      component.runs = [
        ghRun({ job_id: 'j1' }),
        ghRun({ job_id: 'j2', github_context: { owner: 'acme', repo: 'widgets', issue_number: 2 } }),
      ];
      component['buildRunVms']();
      component.selectRun('j2');
      expect(component.activityNarrative.lines).toEqual([]);
      expect(component.showThoughtStreamPanel).toBe(false);
    });

    it('thoughtStreamTitle is Agent thinking when reasoning is present', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), {
        job_id: 'j1',
        status: 'running',
        thinking: 'weighing options',
        status_text: 'Implementing',
      });
      expect(component.thoughtStreamTitle).toBe('Agent thinking');
      expect(component.showThoughtStreamPanel).toBe(true);
    });

    it('renders the Agent thinking panel when jobStatus.thinking is present', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), { job_id: 'j1', status: 'running', thinking: 'weighing the approach' });
      const stream = fixture.nativeElement.querySelector('.thinking-stream');
      expect(stream).not.toBeNull();
      expect(stream?.textContent).toContain('weighing the approach');
    });

    it('hides the thought stream panel when there is no thinking and no narrative', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), { job_id: 'j1', status: 'running' });
      expect(fixture.nativeElement.querySelector('.thought-stream-panel')).toBeNull();
      expect(fixture.nativeElement.querySelector('.thinking-stream')).toBeNull();
    });

    it('renders a polite live region announcing thinking activity, separate from the raw stream', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), { job_id: 'j1', status: 'running', thinking: 'weighing the approach' });
      const live = fixture.nativeElement.querySelector('.thinking-announcement');
      expect(live).not.toBeNull();
      expect(live?.getAttribute('aria-live')).toBe('polite');
      expect(live?.textContent?.trim()).toBe('Agent is thinking…');
      // The announcer never echoes the raw (potentially verbose) stream text.
      expect(live?.textContent).not.toContain('weighing the approach');
    });

    it('renders no thinking live region when there is no thinking text', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), { job_id: 'j1', status: 'running' });
      expect(fixture.nativeElement.querySelector('.thinking-announcement')).toBeNull();
    });

    it('renders Agent activity title and activity stream without thinking', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), {
        job_id: 'j1',
        status: 'running',
        status_text: 'Implementing 1 task(s)',
      });
      const panel = fixture.nativeElement.querySelector('.thought-stream-panel');
      expect(panel).not.toBeNull();
      expect(panel?.textContent).toContain('Agent activity');
      expect(fixture.nativeElement.querySelector('.activity-stream')?.textContent).toContain(
        'Status: Implementing 1 task(s)',
      );
      expect(fixture.nativeElement.querySelector('.reasoning-section')).toBeNull();
    });

    it('renders Reasoning and Activity sections together when both sources exist', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), {
        job_id: 'j1',
        status: 'running',
        thinking: 'weighing the approach',
        status_text: 'Implementing 1 task(s)',
      });
      const panel = fixture.nativeElement.querySelector('.thought-stream-panel');
      expect(panel?.textContent).toContain('Agent thinking');
      expect(fixture.nativeElement.querySelector('.reasoning-section .thinking-stream')?.textContent).toContain(
        'weighing the approach',
      );
      expect(fixture.nativeElement.querySelector('.activity-stream')?.textContent).toContain(
        'Status: Implementing 1 task(s)',
      );
    });

    it('renders a polite activity announcement region for activity-only updates', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), {
        job_id: 'j1',
        status: 'running',
        status_text: 'Building task graph',
      });
      const live = fixture.nativeElement.querySelector('.activity-announcement');
      expect(live?.getAttribute('aria-live')).toBe('polite');
      expect(live?.textContent?.trim()).toBe('Agent activity updated (1)');
      expect(live?.textContent).not.toContain('Building task graph');
    });

    it('mutates the activity live region on each subsequent activity-only update', async () => {
      await setup();
      openRun(ghRun({ job_id: 'j1' }), {
        job_id: 'j1',
        status: 'running',
        status_text: 'Building task graph',
      });
      expect(fixture.nativeElement.querySelector('.activity-announcement')?.textContent?.trim()).toBe(
        'Agent activity updated (1)',
      );
      component.jobStatus = {
        job_id: 'j1',
        status: 'running',
        status_text: 'Implementing 1 task(s)',
      };
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.activity-announcement')?.textContent?.trim()).toBe(
        'Agent activity updated (2)',
      );
    });

    it('renders the questions panel and waiting banner when the run is paused', async () => {
      await setup();
      showRun({ waiting_for_answers: true, pending_questions: [QUESTION] });
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('app-inline-banner[variant="warning"]')).not.toBeNull();
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

    it('shows Resume Run only for Temporal-native pauses with resume_token', async () => {
      await setup();
      showRun({
        status: 'waiting_for_user',
        waiting_for_answers: false,
        resume_token: 'j1:tok-1',
      });
      const el: HTMLElement = fixture.nativeElement;
      expect(el.textContent).toContain('Signal the Temporal workflow');
      expect(el.textContent).toContain('Resume Run');
      expect(el.querySelector('button[aria-label="Resume the coding-team run"]')).not.toBeNull();
    });

    it('hides Resume Run for block-mode pauses without resume_token', async () => {
      await setup();
      showRun({
        status: 'waiting_for_user',
        waiting_for_answers: false,
      });
      const el: HTMLElement = fixture.nativeElement;
      expect(el.textContent).toContain('cannot be resumed from here');
      expect(el.textContent).not.toContain('Resume Run');
      expect(el.querySelector('button[aria-label="Resume the coding-team run"]')).toBeNull();
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
      component.onAnswersSubmitted(resumed as CodingTeamJobStatus);

      expect(component.jobStatus).toEqual(resumed);
      expect(staleSub?.closed).toBe(true);
      expect(component['pollSub']).not.toBeNull();
      expect(component['pollSub']).not.toBe(staleSub);
      expect(component.issueError).toBeNull();
      component['stopPolling']();
    });

    it('onAnswersSubmitted ignores a payload missing the job_id/status discriminators', async () => {
      await setup();
      component.selectedRunId = 'j1';
      component.jobStatus = { job_id: 'j1', status: 'running' };
      component.onAnswersSubmitted({ foo: 'bar' } as unknown as CodingTeamJobStatus);
      // A foreign shape is never folded into jobStatus — the prior status is preserved.
      expect(component.jobStatus).toEqual({ job_id: 'j1', status: 'running' });
      component['stopPolling']();
    });

    it('onAnswersSubmitted ignores a status whose job_id is not the selected run', async () => {
      await setup();
      component.selectedRunId = 'current';
      component.jobStatus = { job_id: 'current', status: 'running' };
      // A slow submit resolving after the user switched runs must not overwrite the current run.
      component.onAnswersSubmitted({ job_id: 'other', status: 'completed' } as CodingTeamJobStatus);
      expect(component.jobStatus).toEqual({ job_id: 'current', status: 'running' });
      expect(component['pollSub']).toBeNull();
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

    it('is a no-op while a resume is already in flight', async () => {
      await setup();
      component.selectedRunId = 'j1';
      component.resumingJob = true;
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
    it('renders an empty state when there are no runs, in a role="status" region', async () => {
      await setup();
      await flushAsync();
      showView('jobs');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('app-empty-state')).not.toBeNull();
      const statusRegions = Array.from(el.querySelectorAll('[role="status"]'));
      expect(statusRegions.some((r) => r.textContent?.includes('No runs yet'))).toBe(true);
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

    it('drops the "In progress" chip when a run is collapsed, even after it finishes', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([ghRun({ job_id: 'r1', status: 'running', github_context: { owner: 'acme', repo: 'widgets', issue_number: 2 } })]),
      );
      await setup();
      await flushAsync();
      // The running run is auto-selected and its issue shows "In progress".
      expect(component.selectedRunId).toBe('r1');
      expect(component.activeRunKeys.has('acme/widgets#2')).toBe(true);

      // Collapsing the run drops the selection *and* its issue number.
      component.toggleRun(component.runs[0]);
      expect(component.selectedRunId).toBeNull();
      expect(component.selectedRunNumber).toBeNull();

      // A later poll that reports the run finished must not re-add a stale chip for the deselected run.
      component['applyRuns']([
        ghRun({ job_id: 'r1', status: 'completed', github_context: { owner: 'acme', repo: 'widgets', issue_number: 2 } }),
      ]);
      expect(component.activeRunKeys.has('acme/widgets#2')).toBe(false);
    });

    it('auto-selects a non-terminal run on first load and starts polling it', async () => {
      apiSpy.listJobs.mockReturnValue(of([ghRun({ job_id: 'j-restore' })]));
      await setup();
      await flushAsync();
      expect(apiSpy.listJobs).toHaveBeenCalledWith(false);
      expect(component.selectedRunId).toBe('j-restore');
      expect(component.selectedRunNumber).toBe(2);
      expect(apiSpy.getJobStatus).toHaveBeenCalledWith('j-restore');
      expect(component.activeRunKeys.has('acme/widgets#2')).toBe(true);
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
      expect(component.activeRunKeys).toEqual(new Set(['acme/widgets#2', 'acme/widgets#3']));
    });

    it('lists runs from every repository the PAT can access, keyed per repo', async () => {
      // Both repos are accessible to the current PAT, so both runs are shown.
      integrationsSpy.getGitHubRepos.mockReturnValue(
        of([
          { ...REPO, full_name: 'acme/other-repo', owner: 'acme', name: 'other-repo' },
          { ...REPO, full_name: 'someone-else/widgets', owner: 'someone-else', name: 'widgets' },
        ]),
      );
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({ github_context: { owner: 'acme', repo: 'other-repo', issue_number: 2 } }),
          ghRun({ job_id: 'other-owner', github_context: { owner: 'someone-else', repo: 'widgets', issue_number: 2 } }),
        ]),
      );
      await setup();
      await flushAsync();
      // Runs are no longer filtered to a single configured repo.
      expect(component.runs.length).toBe(2);
      // The same issue number in two repos yields two distinct chips.
      expect(component.activeRunKeys).toEqual(
        new Set(['acme/other-repo#2', 'someone-else/widgets#2']),
      );
    });

    it('drops runs for repositories not in the accessible-repo list', async () => {
      // The default accessible repo is acme/widgets (REPO). The second run is for a repo the
      // current PAT can no longer reach — e.g. a run from a previous token in shared job
      // storage. /jobs is not PAT-scoped, so the panel must filter it out.
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({ github_context: { owner: 'acme', repo: 'widgets', issue_number: 2 } }),
          ghRun({ job_id: 'stale', status: 'completed', github_context: { owner: 'gone', repo: 'repo', issue_number: 9 } }),
        ]),
      );
      await setup();
      await flushAsync();
      // Only the accessible-repo run survives; the run for the unreachable repo is hidden.
      expect(component.runs.length).toBe(1);
      expect(component.runs[0].github_context?.repo).toBe('widgets');
      expect(component.activeRunKeys.has('gone/repo#9')).toBe(false);
    });

    it('lowercases the repo identity so chips match case-insensitively', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([ghRun({ github_context: { owner: 'Acme', repo: 'Widgets', issue_number: 2 } })]),
      );
      await setup();
      await flushAsync();
      expect(component.selectedRunId).toBe('j-run');
      expect(component.activeRunKeys.has('acme/widgets#2')).toBe(true);
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
      expect(component.activeRunKeys.size).toBe(0);
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
      expandFirstRepo();
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

    it('selectRun clears selectedRunNumber when the chosen run carries no issue number', async () => {
      await setup();
      // A run present in `runs` but missing github_context (defensive: the list is normally
      // pre-filtered to issue-bearing runs) must not leave a previous run's number stale.
      component.runs = [{ job_id: 'no-issue', status: 'running' } as CodingTeamJobListItem];
      component.selectedRunNumber = 5;
      component.selectRun('no-issue');
      expect(component.selectedRunId).toBe('no-issue');
      expect(component.selectedRunNumber).toBeNull();
      component['stopPolling']();
    });

    it('a stale snapshot cannot wipe the chip of a just-started, still-running run', async () => {
      await setup();
      component.selectedRunId = 'mine';
      component.selectedRunNumber = 9;
      component.selectedRunOwner = 'acme';
      component.selectedRunRepo = 'widgets';
      component.jobStatus = { job_id: 'mine', status: 'running' };
      component['applyRuns']([]);
      expect(component.activeRunKeys.has('acme/widgets#9')).toBe(true);
    });

    it('does not re-add the chip for a selected run that has finished', async () => {
      await setup();
      component.selectedRunId = 'mine';
      component.selectedRunNumber = 9;
      component.selectedRunOwner = 'acme';
      component.selectedRunRepo = 'widgets';
      component.jobStatus = { job_id: 'mine', status: 'completed' };
      component['applyRuns']([]);
      expect(component.activeRunKeys.has('acme/widgets#9')).toBe(false);
    });

    it('drops the chip once the snapshot reports the selected run terminal, even if the polled status is stale', async () => {
      await setup();
      component['initialRunsLoad'] = false;
      component.selectedRunId = 'r1';
      component.selectedRunNumber = 5;
      component.selectedRunOwner = 'acme';
      component.selectedRunRepo = 'widgets';
      // Polled status lags behind the server: still "running" though the run has finished.
      component.jobStatus = { job_id: 'r1', status: 'running' };
      component['applyRuns']([
        ghRun({ job_id: 'r1', status: 'completed', github_context: { owner: 'acme', repo: 'widgets', issue_number: 5 } }),
      ]);
      // The fresh snapshot is trusted: #5 is no longer in progress and the run sits under Recent.
      expect(component.activeRunKeys.has('acme/widgets#5')).toBe(false);
      expect(component.recentRuns.map((r) => r.job_id)).toEqual(['r1']);
    });

    it('drops the chip and refreshes the list when the poller observes a terminal status', async () => {
      await setup();
      component.selectedRunId = 'j1';
      component.selectedRunNumber = 7;
      component.selectedRunOwner = 'acme';
      component.selectedRunRepo = 'widgets';
      component.activeRunKeys.add('acme/widgets#7');
      apiSpy.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'completed' }));
      apiSpy.listJobs.mockReturnValue(of([]));
      component['startPolling']('j1');
      await flushAsync();
      expect(component.jobStatus?.status).toBe('completed');
      expect(component.activeRunKeys.has('acme/widgets#7')).toBe(false);
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
  // PR-driven runs (address-comments remediation jobs: github_context carries
  // pr_number instead of issue_number)
  // -------------------------------------------------------------------------

  describe('PR-driven runs', () => {
    it('includes a PR-context job in the Runs list instead of filtering it out', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({
            job_id: 'pr-job',
            status: 'running',
            github_context: { owner: 'acme', repo: 'widgets', pr_number: 42, pr_url: 'https://example.com/pull/42' },
          }),
        ]),
      );
      await setup();
      await flushAsync();
      showView('jobs');
      expect(component.runs.map((r) => r.job_id)).toEqual(['pr-job']);
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelectorAll('.coding-run-item').length).toBe(1);
      expect(el.textContent).toContain('acme/widgets#42 (PR)');
    });

    it('selects a PR-context run, deriving its kind and number', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({
            job_id: 'pr-job',
            status: 'running',
            github_context: { owner: 'acme', repo: 'widgets', pr_number: 42, pr_url: 'https://example.com/pull/42' },
          }),
        ]),
      );
      await setup();
      await flushAsync();
      // Non-terminal runs are auto-selected on first load, same as an issue-driven run.
      expect(component.selectedRunId).toBe('pr-job');
      expect(component.selectedRunNumber).toBe(42);
      expect(component.selectedRunKind).toBe('pr');
      expect(component.selectedRunOwner).toBe('acme');
      expect(component.selectedRunRepo).toBe('widgets');
    });

    it('keys the "in progress" chip for a PR run separately from an issue of the same number', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({
            job_id: 'pr-job',
            status: 'running',
            github_context: { owner: 'acme', repo: 'widgets', pr_number: 2 },
          }),
        ]),
      );
      await setup();
      await flushAsync();
      expect(component.activeRunKeys.has('acme/widgets#pr-2')).toBe(true);
      // A same-numbered issue is a distinct identity and must not be flagged in progress.
      expect(component.activeRunKeys.has('acme/widgets#2')).toBe(false);
    });

    it('makes a paused PR-remediation run reachable for answering questions, like an issue run', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({
            job_id: 'pr-paused',
            status: 'waiting_for_user',
            waiting_for_answers: true,
            github_context: { owner: 'acme', repo: 'widgets', pr_number: 7 },
          }),
        ]),
      );
      apiSpy.getJobStatus.mockReturnValue(
        of({
          job_id: 'pr-paused',
          status: 'waiting_for_user',
          waiting_for_answers: true,
          pending_questions: [
            { id: 'q1', question_text: 'Which approach?', options: [{ id: 'a', label: 'A' }], required: true, source: 'tech_lead' },
          ],
          github_context: { owner: 'acme', repo: 'widgets', pr_number: 7 },
        }),
      );
      await setup();
      await flushAsync();
      showView('jobs');
      fixture.detectChanges();
      // Paused runs are preferred by auto-select, so this run's answer controls are on screen.
      expect(component.selectedRunId).toBe('pr-paused');
      expect(fixture.nativeElement.querySelector('app-pending-questions')).not.toBeNull();
    });

    it('does not offer "Run again" for a finished PR-remediation run', async () => {
      apiSpy.listJobs.mockReturnValue(
        of([
          ghRun({
            job_id: 'pr-done',
            status: 'completed',
            github_context: { owner: 'acme', repo: 'widgets', pr_number: 3 },
          }),
        ]),
      );
      await setup();
      await flushAsync();
      showView('jobs');
      component.toggleRun(component.recentRuns[0]);
      await flushAsync();
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.run-detail__retry')).toBeNull();
      component.retrySelectedRun();
      expect(integrationsSpy.runGitHubIssue).not.toHaveBeenCalled();
    });

    it('backfills selectedRunKind from the polled status even when selectedRunNumber is already set', async () => {
      await setup();
      // A run pre-selected with only its number known (no kind yet) — startPolling must not
      // skip the kind backfill just because the number is already present.
      component.selectedRunId = 'pr-job';
      component.selectedRunNumber = 9;
      component.selectedRunKind = null;
      apiSpy.getJobStatus.mockReturnValue(
        of({ job_id: 'pr-job', status: 'running', github_context: { owner: 'acme', repo: 'widgets', pr_number: 9 } }),
      );
      component['startPolling']('pr-job');
      await flushAsync();
      expect(component.selectedRunKind).toBe('pr');
      expect(component.selectedRunNumber).toBe(9);
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

    it('re-runs the selected run\'s issue in the same repository and selects the new run', async () => {
      await setup();
      expandFirstRepo();
      component.selectedRunNumber = 5;
      component.selectedRunOwner = 'acme';
      component.selectedRunRepo = 'widgets';
      integrationsSpy.runGitHubIssue.mockReturnValue(
        of({ job_id: 'j-retry', issue_number: 5, issue_url: 'u', status: 'queued', message: '' }),
      );
      component.retrySelectedRun();
      expect(integrationsSpy.runGitHubIssue).toHaveBeenCalledWith({
        issue_number: 5,
        owner: 'acme',
        repo: 'widgets',
      });
      expect(component.selectedRunId).toBe('j-retry');
      expect(component.isIssueInProgress(issueWith({ number: 5 }))).toBe(true);
    });

    it('derives the retry repository from the polled status when not tracked', async () => {
      await setup();
      component.selectedRunOwner = '';
      component.selectedRunRepo = '';
      component.jobStatus = { job_id: 'j1', status: 'failed', github_context: { owner: 'acme', repo: 'widgets', issue_number: 6 } };
      integrationsSpy.runGitHubIssue.mockReturnValue(throwError(() => ({ error: { detail: 'nope' } })));
      component.retrySelectedRun();
      expect(integrationsSpy.runGitHubIssue).toHaveBeenCalledWith({
        issue_number: 6,
        owner: 'acme',
        repo: 'widgets',
      });
      expect(component.issueError).toBe('nope');
      expect(component.runningIssue).toBe(false);
    });

    it('is a no-op when the run\'s repository is unknown', async () => {
      await setup();
      component.selectedRunNumber = 5;
      component.selectedRunOwner = '';
      component.selectedRunRepo = '';
      component.jobStatus = null;
      component.retrySelectedRun();
      expect(integrationsSpy.runGitHubIssue).not.toHaveBeenCalled();
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

    it('reports a lost connection after repeated status-poll failures', async () => {
      vi.useFakeTimers();
      try {
        await setup();
        component.selectedRunId = 'j1';
        apiSpy.getJobStatus.mockReturnValue(throwError(() => new Error('network down')));
        component['startPolling']('j1');
        // The status poll fires immediately, then every 5s; it gives up after 3 consecutive
        // failures and surfaces the lost-connection error.
        await vi.advanceTimersByTimeAsync(0);
        await vi.advanceTimersByTimeAsync(5000);
        await vi.advanceTimersByTimeAsync(5000);
        expect(component.issueError).toBe('Lost connection to the coding team — status polling failed.');
        // The run detail surfaces the same error instead of an indefinite "Starting…" spinner.
        expect(component.jobStatusError).toBe('Lost connection to the coding team — status polling failed.');
        // The poller tore down its own timer once the error budget was exhausted.
        expect(component['pollSub']?.closed).toBe(true);
      } finally {
        component['stopPolling']();
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

    it('cancels the thinking-announcement settle timer on destroy', async () => {
      await setup();
      component.jobStatus = { job_id: 'j1', status: 'running', thinking: 'Step one' };
      expect(component.thinkingAnnouncementPending).toBe(true);
      fixture.destroy();
      expect(component.thinkingAnnouncementPending).toBe(false);
    });

    it('settles a whitespace-only thinking value to an empty announcement, not "0 lines"', async () => {
      vi.useFakeTimers();
      try {
        await setup();
        component.jobStatus = { job_id: 'j1', status: 'running', thinking: '   ' };
        expect(component.thinkingAnnouncement).toBe('Agent is thinking…');
        await vi.advanceTimersByTimeAsync(1500);
        expect(component.thinkingAnnouncement).toBe('');
      } finally {
        vi.useRealTimers();
      }
    });
  });

  // -------------------------------------------------------------------------
  // Pull Requests tab — open PRs + "address unresolved comments" action
  // -------------------------------------------------------------------------
  describe('Pull Requests tab', () => {
    /** Expand a repo, switch to the pulls tab, and render. */
    function openPullsForRepo(): void {
      component.toggleRepo(component.repos[0]);
      showView('pulls');
    }

    it('auto-loads the expanded repo\'s open PRs when the tab opens', async () => {
      integrationsSpy.getGitHubPullRequests.mockReturnValue(of([ghPull(1), ghPull(2)]));
      await setup();
      openPullsForRepo();
      expect(integrationsSpy.getGitHubPullRequests).toHaveBeenCalledWith({ owner: 'acme', repo: 'widgets' });
      expect(component.pulls.length).toBe(2);
      expect(component.pullsLoaded).toBe(true);
    });

    it('renders a row with a play_arrow "Address comments" button per PR', async () => {
      integrationsSpy.getGitHubPullRequests.mockReturnValue(of([ghPull(7)]));
      await setup();
      openPullsForRepo();
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelectorAll('.pull-row').length).toBe(1);
      const icons = Array.from(el.querySelectorAll('.pull-row__actions mat-icon')).map((i) => i.textContent?.trim());
      expect(icons).toContain('play_arrow');
    });

    it('the per-PR "Address comments" button is wired to addressPrComments (template click)', async () => {
      // Clicking the real button — rather than calling the method directly, as the
      // behavioral tests below do — is what pins the template's (click) binding: a
      // dropped or mis-bound handler would leave every one of those tests passing.
      integrationsSpy.getGitHubPullRequests.mockReturnValue(of([ghPull(7)]));
      integrationsSpy.addressPrComments.mockReturnValue(
        of({ job_id: 'a1', pr_number: 7, pr_url: 'u', unresolved_comment_count: 2, status: 'pending', message: '' }),
      );
      await setup();
      openPullsForRepo();
      const btn: HTMLButtonElement | null = fixture.nativeElement.querySelector('.pull-row__actions button');
      expect(btn).toBeTruthy();
      btn!.click();
      fixture.detectChanges();
      expect(integrationsSpy.addressPrComments).toHaveBeenCalledTimes(1);
      expect(integrationsSpy.addressPrComments).toHaveBeenCalledWith(7, { owner: 'acme', repo: 'widgets' });
    });

    it('disables the per-PR button and shows "Starting…" while its job is in flight', async () => {
      integrationsSpy.getGitHubPullRequests.mockReturnValue(of([ghPull(7)]));
      // Never-completing observable so the in-flight flag stays set after the click.
      integrationsSpy.addressPrComments.mockReturnValue(new Subject());
      await setup();
      openPullsForRepo();
      const btn: HTMLButtonElement | null = fixture.nativeElement.querySelector('.pull-row__actions button');
      expect(btn).toBeTruthy();
      expect(btn!.disabled).toBe(false);

      btn!.click();
      fixture.detectChanges();

      // The template binds [disabled]="isAddressingPr(pr)", so the native button is
      // actually disabled — a second click cannot re-submit the same PR.
      expect(btn!.disabled).toBe(true);
      expect(btn!.textContent).toContain('Starting…');
      btn!.click();
      expect(integrationsSpy.addressPrComments).toHaveBeenCalledTimes(1);
    });

    it('prompts to pick a repo when none is expanded', async () => {
      await setup();
      showView('pulls');
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.pulls-panel__repo-prompt')).not.toBeNull();
      expect(integrationsSpy.getGitHubPullRequests).not.toHaveBeenCalled();
    });

    it('shows an empty state when the repo has no open PRs', async () => {
      integrationsSpy.getGitHubPullRequests.mockReturnValue(of([]));
      await setup();
      openPullsForRepo();
      expect(fixture.nativeElement.querySelector('.pulls-panel__empty')).not.toBeNull();
    });

    it('does not re-fetch on every tab switch when the repo has no open PRs', async () => {
      // The auto-load guard keys off `pullsLoaded`, not `pulls.length`: a repo whose
      // open-PR list is legitimately empty is still LOADED, so leaving and re-entering
      // the tab must not re-issue the request.
      integrationsSpy.getGitHubPullRequests.mockReturnValue(of([]));
      await setup();
      openPullsForRepo();
      expect(component.pulls).toEqual([]);
      expect(component.pullsLoaded).toBe(true);
      const loadedCalls = integrationsSpy.getGitHubPullRequests.mock.calls.length;

      showView('jobs');
      showView('pulls');
      showView('jobs');
      showView('pulls');
      expect(integrationsSpy.getGitHubPullRequests.mock.calls.length).toBe(loadedCalls);
    });

    it('retries the auto-load on a later tab switch when the first load failed', async () => {
      // The flag must not suppress a RETRY: `loadPulls` sets `pullsLoaded` only on
      // success, so a failed load leaves the guard open and re-entering the tab
      // tries again. This is what keeps the `pullsLoaded` guard from stranding a
      // repo on a transient error with no way back short of a manual Refresh.
      integrationsSpy.getGitHubPullRequests.mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
      await setup();
      openPullsForRepo();
      expect(component.pullError).toBe('boom');
      expect(component.pullsLoaded).toBe(false);
      const failedCalls = integrationsSpy.getGitHubPullRequests.mock.calls.length;

      integrationsSpy.getGitHubPullRequests.mockReturnValue(of([ghPull(7)]));
      showView('jobs');
      showView('pulls');
      expect(integrationsSpy.getGitHubPullRequests.mock.calls.length).toBe(failedCalls + 1);
      expect(component.pulls.length).toBe(1);
      expect(component.pullsLoaded).toBe(true);
    });

    it('surfaces a load error in an inline banner', async () => {
      integrationsSpy.getGitHubPullRequests.mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
      await setup();
      openPullsForRepo();
      expect(component.pullError).toBe('boom');
    });

    it('switching repos mid-load lets the new repo auto-load its own PRs, and the stale response cannot overwrite it', async () => {
      const slowA = new Subject<GitHubPullRequestItem[]>();
      integrationsSpy.getGitHubPullRequests.mockReturnValueOnce(slowA);
      await setup();
      const repoB: GitHubRepoItem = { ...component.repos[0], name: 'gadgets', full_name: 'acme/gadgets' };
      component.repos = [component.repos[0], repoB];

      // Expand repo A and open the Pulls tab: loadPulls() fires and is left in flight.
      component.toggleRepo(component.repos[0]);
      showView('pulls');
      expect(component.loadingPulls).toBe(true);

      // Switch to repo B before A's request settles.
      integrationsSpy.getGitHubPullRequests.mockReturnValueOnce(of([ghPull(9)]));
      component.toggleRepo(repoB);

      // The switch must not leave `loadingPulls` stuck true — B's own Pulls-tab
      // auto-load guard (`!loadingPulls`) would otherwise refuse to fire.
      expect(component.loadingPulls).toBe(false);
      showView('pulls');
      expect(integrationsSpy.getGitHubPullRequests).toHaveBeenCalledWith({ owner: 'acme', repo: 'gadgets' });
      expect(component.pulls.map((p) => p.number)).toEqual([9]);

      // A's stale response arriving late must not clobber B's already-loaded state.
      slowA.next([ghPull(1), ghPull(2)]);
      slowA.complete();
      expect(component.pulls.map((p) => p.number)).toEqual([9]);
    });

    it('addressPrComments starts the job, toasts, and clears the in-flight guard', async () => {
      integrationsSpy.getGitHubPullRequests.mockReturnValue(of([ghPull(7)]));
      integrationsSpy.addressPrComments.mockReturnValue(
        of({ job_id: 'a1', pr_number: 7, pr_url: 'u', unresolved_comment_count: 2, status: 'pending', message: '' }),
      );
      await setup();
      openPullsForRepo();
      component.addressPrComments(ghPull(7));
      expect(integrationsSpy.addressPrComments).toHaveBeenCalledWith(7, { owner: 'acme', repo: 'widgets' });
      expect(notificationsSpy.saved).toHaveBeenCalled();
      expect(component.isAddressingPr(ghPull(7))).toBe(false);
    });

    it('addressPrComments guards against a double-submit while in flight', async () => {
      integrationsSpy.getGitHubPullRequests.mockReturnValue(of([ghPull(7)]));
      // Never-completing observable so the in-flight flag stays set.
      integrationsSpy.addressPrComments.mockReturnValue(new Subject());
      await setup();
      openPullsForRepo();
      component.addressPrComments(ghPull(7));
      component.addressPrComments(ghPull(7));
      expect(integrationsSpy.addressPrComments).toHaveBeenCalledTimes(1);
      expect(component.isAddressingPr(ghPull(7))).toBe(true);
    });

    it('addressPrComments surfaces an error and clears the guard', async () => {
      integrationsSpy.getGitHubPullRequests.mockReturnValue(of([ghPull(7)]));
      integrationsSpy.addressPrComments.mockReturnValue(throwError(() => ({ error: { detail: 'nope' } })));
      await setup();
      openPullsForRepo();
      component.addressPrComments(ghPull(7));
      expect(component.pullError).toBe('nope');
      expect(component.isAddressingPr(ghPull(7))).toBe(false);
    });
  });

  describe('focus management (issue #7918)', () => {
    it('selectIssue moves focus into the confirm panel, not left on the row button', async () => {
      vi.useFakeTimers();
      try {
        await setup();
        showView('github');
        expandFirstRepo();
        const issue = component.issues[0];
        const el: HTMLElement = fixture.nativeElement;
        const row = el.querySelector<HTMLButtonElement>('.github-issue-row');
        expect(row).not.toBeNull();

        component.selectIssue(issue);
        fixture.detectChanges();
        await vi.advanceTimersByTimeAsync(0);
        fixture.detectChanges();

        const panel = el.querySelector('.github-confirm-panel');
        expect(panel).not.toBeNull();
        expect(panel?.contains(document.activeElement)).toBe(true);
        expect(document.activeElement).not.toBe(row);

        // The panel's accessible name comes from its heading via aria-labelledby, not subtree
        // fallback on a plain div — assert the wiring actually points at the rendered heading.
        const labelledBy = panel?.getAttribute('aria-labelledby');
        expect(labelledBy).toBe(component.confirmPanelHeadingId(issue.number));
        const heading = el.querySelector(`[id="${labelledBy}"]`);
        expect(heading?.tagName).toBe('H3');
        expect(heading?.textContent).toContain('Start AI coding on this issue?');
      } finally {
        vi.useRealTimers();
      }
    });

    it('cancelSelection returns focus to the originating issue row button', async () => {
      vi.useFakeTimers();
      try {
        await setup();
        showView('github');
        expandFirstRepo();
        const issue = component.issues[0];
        const el: HTMLElement = fixture.nativeElement;
        // Capture the row before selecting — aria-controls is only present on the selected row,
        // so it can't be used to relocate the row once cancelSelection() deselects it.
        const row = el.querySelector<HTMLButtonElement>('.github-issue-row');
        expect(row).not.toBeNull();

        component.selectIssue(issue);
        fixture.detectChanges();
        await vi.advanceTimersByTimeAsync(0);
        fixture.detectChanges();

        component.cancelSelection();
        fixture.detectChanges();
        await vi.advanceTimersByTimeAsync(0);
        fixture.detectChanges();

        expect(document.activeElement).toBe(row);
        expect(row?.getAttribute('aria-controls')).toBeNull();
        expect(el.querySelector('.github-confirm-panel')).toBeNull();
      } finally {
        vi.useRealTimers();
      }
    });

    it('cancels the pending focus timer on destroy', async () => {
      await setup();
      showView('github');
      expandFirstRepo();
      component.selectIssue(component.issues[0]);
      expect(component['focusTimer']).not.toBeNull();
      fixture.destroy();
      expect(component['focusTimer']).toBeNull();
    });

    it('cancelSelection falls back to the issue list when the originating row has been filtered out', async () => {
      vi.useFakeTimers();
      try {
        await setup();
        showView('github');
        expandFirstRepo();
        const issue = component.issues[0]; // "Issue 1"

        component.selectIssue(issue);
        fixture.detectChanges();
        await vi.advanceTimersByTimeAsync(0);
        fixture.detectChanges();

        // Narrow the search to a term that excludes the selected issue's own row, mirroring
        // what a real keystroke does (component.onIssueSearchChange()).
        component.issueSearch = 'Issue 2';
        component.onIssueSearchChange();
        fixture.detectChanges();
        const el: HTMLElement = fixture.nativeElement;
        expect(el.querySelector(`[data-issue-number="${component.issueRowKey(issue.number)}"]`)).toBeNull();

        // The row lookup can't find its target, so focus falls back to .github-issues-list
        // rather than dropping to <body> — this pins that fallback, not just a non-throw.
        expect(() => component.cancelSelection()).not.toThrow();
        fixture.detectChanges();
        await vi.advanceTimersByTimeAsync(0);
        fixture.detectChanges();

        expect(el.querySelector('.github-confirm-panel')).toBeNull();
        expect(document.activeElement).toBe(el.querySelector('.github-issues-list'));
      } finally {
        vi.useRealTimers();
      }
    });

    it('cancelSelection falls back further to the search input when the search matches nothing at all', async () => {
      vi.useFakeTimers();
      try {
        await setup();
        showView('github');
        expandFirstRepo();
        const issue = component.issues[0];

        component.selectIssue(issue);
        fixture.detectChanges();
        await vi.advanceTimersByTimeAsync(0);
        fixture.detectChanges();

        // A search matching zero issues removes .github-issues-list entirely (the template's
        // "no matches" empty state renders instead), so the first-level fallback is also absent.
        component.issueSearch = 'zzz-no-match';
        component.onIssueSearchChange();
        fixture.detectChanges();
        const el: HTMLElement = fixture.nativeElement;
        expect(el.querySelector('.github-issues-list')).toBeNull();

        expect(() => component.cancelSelection()).not.toThrow();
        fixture.detectChanges();
        await vi.advanceTimersByTimeAsync(0);
        fixture.detectChanges();

        const searchInput = el.querySelector('.github-search-field input');
        expect(searchInput).not.toBeNull();
        expect(document.activeElement).toBe(searchInput);
      } finally {
        vi.useRealTimers();
      }
    });
  });

  describe('focus management — run detail reveal', () => {
    /** Put a run into the Jobs accordion (unexpanded) so its row exists to toggle. */
    function seedRunningRow(run: CodingTeamJobListItem): void {
      component.runs = [run];
      component.runningRuns = [run];
      component.recentRuns = [];
      component['buildRunVms']();
      fixture.detectChanges();
    }

    it(
      'toggleRun lands focus in the hoisted run-detail container while still "Starting…", ' +
        'and it survives the branch swap once the first status snapshot arrives',
      async () => {
        const statusSubject = new Subject<CodingTeamJobStatus>();
        apiSpy.getJobStatus.mockReturnValue(statusSubject.asObservable());
        await setup();
        // Drain the initial (empty) runs poll before manually seeding a row — otherwise it lands
        // later and overwrites the seeded run with the default empty list.
        await flushAsync();
        const run = ghRun({ job_id: 'r1', status: 'running' });
        seedRunningRow(run);

        component.toggleRun(run);
        fixture.detectChanges();
        await flushAsync();
        fixture.detectChanges();

        const el: HTMLElement = fixture.nativeElement;
        let container = el.querySelector('[id="run-detail-r1"]');
        expect(container).not.toBeNull();
        expect(component.jobStatus).toBeNull();
        expect(el.querySelector('app-loading-spinner')).not.toBeNull();
        expect(container!.contains(document.activeElement)).toBe(true);

        // The first status snapshot lands: Angular destroys the pending branch and creates the
        // populated one as a sibling — the hoisted container is the one node that survives, so
        // the focus already placed there is never lost to the branch swap.
        statusSubject.next({ job_id: 'r1', status: 'running', phase: 'coding' });
        fixture.detectChanges();

        container = el.querySelector('[id="run-detail-r1"]');
        expect(container).not.toBeNull();
        expect(el.querySelector('app-loading-spinner')).toBeNull();
        expect(container!.contains(document.activeElement)).toBe(true);
      },
    );

    it('collapsing the expanded run row does not move focus away from it', async () => {
      await setup();
      await flushAsync(); // drain the initial (empty) runs poll before seeding
      const run = ghRun({ job_id: 'r1', status: 'running' });
      seedRunningRow(run);

      component.toggleRun(run);
      fixture.detectChanges();
      await flushAsync();
      fixture.detectChanges();

      const el: HTMLElement = fixture.nativeElement;
      const row = el.querySelector<HTMLButtonElement>('.coding-run-item');
      expect(row).not.toBeNull();
      // Simulate the row still holding focus (e.g. a mouse click) right before it collapses.
      row!.focus();
      expect(document.activeElement).toBe(row);

      component.toggleRun(run);
      fixture.detectChanges();

      expect(component.selectedRunId).toBeNull();
      expect(document.activeElement).toBe(row);
    });

    it('autoSelectRun never moves focus on a first list load', async () => {
      apiSpy.listJobs.mockReturnValue(of([ghRun({ job_id: 'auto-run', status: 'running' })]));
      const before = document.activeElement;
      await setup();
      await flushAsync();
      expect(component.selectedRunId).toBe('auto-run');
      expect(document.activeElement).toBe(before);
    });

    it('a later status snapshot for an already-expanded run never moves focus', async () => {
      await setup();
      await flushAsync(); // drain the initial (empty) runs poll before seeding
      const run = ghRun({ job_id: 'r1', status: 'running' });
      seedRunningRow(run);

      component.toggleRun(run);
      fixture.detectChanges();
      await flushAsync();
      fixture.detectChanges();
      expect(component.jobStatus).not.toBeNull();

      const el: HTMLElement = fixture.nativeElement;
      const legend = el.querySelector<HTMLElement>('.jobs-panel__legend');
      expect(legend).not.toBeNull();
      legend!.focus();
      expect(document.activeElement).toBe(legend);

      // A later poll tick delivers a fresh snapshot for the same run — this is exactly what the
      // real poller callback does (`this.jobStatus = status`), never routed back through a
      // focus-moving call.
      component.jobStatus = { job_id: 'r1', status: 'running', phase: 'reviewing' };
      fixture.detectChanges();

      expect(document.activeElement).toBe(legend);
    });

    it('confirmAndRun moves focus into the revealed run detail', async () => {
      await setup();
      await flushAsync(); // drain the initial (empty) runs poll
      expandFirstRepo();
      const issue = component.issues[0];
      const newRun = ghRun({
        job_id: 'j-new',
        status: 'running',
        github_context: { owner: 'acme', repo: 'widgets', issue_number: issue.number },
      });
      // Seed the run's row ahead of time (as if the runs-list refresh had already landed), so its
      // detail's hoisted container exists in the DOM as soon as selectedRunId flips to it — the
      // very next (synchronous) render, well before the deferred focus move's timer fires. This
      // isolates the focus-wiring assertion from the runs-list refetch's own async timing.
      seedRunningRow(newRun);
      apiSpy.listJobs.mockReturnValue(of([newRun]));
      integrationsSpy.runGitHubIssue.mockReturnValue(
        of({ job_id: 'j-new', issue_number: issue.number, issue_url: 'u', status: 'queued', message: '' }),
      );

      component.selectIssue(issue);
      component.confirmAndRun();
      fixture.detectChanges();
      await flushAsync();
      fixture.detectChanges();

      const el: HTMLElement = fixture.nativeElement;
      const container = el.querySelector('[id="run-detail-j-new"]');
      expect(container).not.toBeNull();
      expect(component.selectedRunId).toBe('j-new');
      expect(container!.contains(document.activeElement)).toBe(true);
    });

    it('retrySelectedRun moves focus into the revealed run detail', async () => {
      await setup();
      await flushAsync(); // drain the initial (empty) runs poll
      component.selectedRunNumber = 5;
      component.selectedRunOwner = 'acme';
      component.selectedRunRepo = 'widgets';
      const retryRun = ghRun({
        job_id: 'j-retry',
        status: 'running',
        github_context: { owner: 'acme', repo: 'widgets', issue_number: 5 },
      });
      // Same reasoning as confirmAndRun above: seed the run's row ahead of time so its detail's
      // hoisted container exists as soon as retrySelectedRun flips selectedRunId to it.
      seedRunningRow(retryRun);
      apiSpy.listJobs.mockReturnValue(of([retryRun]));
      integrationsSpy.runGitHubIssue.mockReturnValue(
        of({ job_id: 'j-retry', issue_number: 5, issue_url: 'u', status: 'queued', message: '' }),
      );

      component.retrySelectedRun();
      fixture.detectChanges();
      await flushAsync();
      fixture.detectChanges();

      const el: HTMLElement = fixture.nativeElement;
      const container = el.querySelector('[id="run-detail-j-retry"]');
      expect(container).not.toBeNull();
      expect(component.selectedRunId).toBe('j-retry');
      expect(container!.contains(document.activeElement)).toBe(true);
    });

    it('reuses the shared focus timer across reveal paths, superseding a still-pending move, and cancels it on destroy', async () => {
      await setup();
      await flushAsync(); // drain the initial (empty) runs poll before seeding
      const clearTimeoutSpy = vi.spyOn(globalThis, 'clearTimeout');
      const run = ghRun({ job_id: 'r1', status: 'running' });
      seedRunningRow(run);

      component.toggleRun(run);
      const firstTimer = component['focusTimer'];
      expect(firstTimer).not.toBeNull();

      // retrySelectedRun schedules its own focus move through the same private field — proving
      // it's genuinely shared across reveal paths (not one timer per call site) — and, because
      // the first move is still pending at this point, that scheduling the second one clears it
      // rather than leaving both to fire.
      component.selectedRunNumber = 5;
      component.selectedRunOwner = 'acme';
      component.selectedRunRepo = 'widgets';
      integrationsSpy.runGitHubIssue.mockReturnValue(
        of({ job_id: 'r2', issue_number: 5, issue_url: 'u', status: 'queued', message: '' }),
      );
      component.retrySelectedRun();

      expect(clearTimeoutSpy).toHaveBeenCalledWith(firstTimer);
      expect(component['focusTimer']).not.toBeNull();
      expect(component['focusTimer']).not.toBe(firstTimer);

      fixture.destroy();
      expect(component['focusTimer']).toBeNull();
      clearTimeoutSpy.mockRestore();
    });
  });
});
