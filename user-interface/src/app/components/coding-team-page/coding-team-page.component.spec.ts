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
  }));
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
});
