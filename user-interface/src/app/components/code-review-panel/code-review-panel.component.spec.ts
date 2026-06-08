import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { vi } from 'vitest';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { CodeReviewPanelComponent } from './code-review-panel.component';
import type { GitHubConfigResponse, GitHubPullRequestItem } from '../../models/integrations.model';

function makePulls(count: number): GitHubPullRequestItem[] {
  return Array.from({ length: count }, (_, i) => ({
    number: i + 1,
    title: `PR ${i + 1}`,
    body_preview: `body ${i + 1}`,
    author: 'octocat',
    html_url: `https://example.com/pull/${i + 1}`,
    head: `feature-${i + 1}`,
    base: 'main',
    draft: i % 2 === 0,
    labels: i % 2 === 0 ? ['needs-review'] : [],
    updated_at: '2026-01-01T00:00:00Z',
  }));
}

const CONFIGURED: GitHubConfigResponse = {
  enabled: true,
  token_configured: true,
  owner: 'acme',
  repo: 'widgets',
  default_label: 'ai',
};

const UNCONFIGURED: GitHubConfigResponse = {
  enabled: false,
  token_configured: false,
  owner: '',
  repo: '',
  default_label: '',
};

describe('CodeReviewPanelComponent', () => {
  let component: CodeReviewPanelComponent;
  let fixture: ComponentFixture<CodeReviewPanelComponent>;
  let apiSpy: {
    health: ReturnType<typeof vi.fn>;
    getJobStatus: ReturnType<typeof vi.fn>;
  };
  let integrationsSpy: {
    getGitHubConfig: ReturnType<typeof vi.fn>;
    getGitHubPullRequests: ReturnType<typeof vi.fn>;
    runGitHubReviewPr: ReturnType<typeof vi.fn>;
  };

  async function setup(): Promise<void> {
    await TestBed.configureTestingModule({
      imports: [CodeReviewPanelComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: CodingTeamApiService, useValue: apiSpy },
        { provide: IntegrationsApiService, useValue: integrationsSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(CodeReviewPanelComponent);
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
      getGitHubPullRequests: vi.fn().mockReturnValue(of(makePulls(3))),
      runGitHubReviewPr: vi.fn().mockReturnValue(
        of({ job_id: 'j1', pr_number: 1, pr_url: 'https://example.com/pull/1', status: 'pending', message: '' }),
      ),
    };
  });

  it('should create and load pull requests when configured', async () => {
    await setup();
    expect(component.githubConfigured).toBe(true);
    expect(integrationsSpy.getGitHubPullRequests).toHaveBeenCalled();
    expect(component.pulls.length).toBe(3);
    expect(component.pullsLoaded).toBe(true);
  });

  it('does not load pull requests when GitHub is unconfigured', async () => {
    integrationsSpy.getGitHubConfig.mockReturnValue(of(UNCONFIGURED));
    await setup();
    expect(component.githubConfigured).toBe(false);
    expect(integrationsSpy.getGitHubPullRequests).not.toHaveBeenCalled();
  });

  it('treats a config check error as unconfigured', async () => {
    integrationsSpy.getGitHubConfig.mockReturnValue(throwError(() => new Error('boom')));
    await setup();
    expect(component.githubConfigured).toBe(false);
    expect(component.loadingConfig).toBe(false);
  });

  it('surfaces a load error from the PR list', async () => {
    integrationsSpy.getGitHubPullRequests.mockReturnValue(
      throwError(() => ({ error: { detail: 'rate limited' } })),
    );
    await setup();
    expect(component.pullError).toBe('rate limited');
    expect(component.loadingPulls).toBe(false);
  });

  it('paginates the PR list client-side', async () => {
    integrationsSpy.getGitHubPullRequests.mockReturnValue(of(makePulls(25)));
    await setup();
    expect(component.pagedPulls.length).toBe(10);
    component.onPageChange({ pageIndex: 1, pageSize: 10, length: 25 });
    expect(component.pageIndex).toBe(1);
    expect(component.pagedPulls[0].number).toBe(11);
  });

  it('selects and cancels a pull request', async () => {
    await setup();
    const pull = component.pulls[0];
    component.selectPull(pull);
    expect(component.selectedPull).toBe(pull);
    component.cancelSelection();
    expect(component.selectedPull).toBeNull();
  });

  it('starts a review on confirm and clears the selection', async () => {
    await setup();
    component.selectPull(component.pulls[0]);
    component.confirmAndReview();
    expect(integrationsSpy.runGitHubReviewPr).toHaveBeenCalledWith({ pr_number: 1 });
    expect(component.activeJob?.job_id).toBe('j1');
    expect(component.selectedPull).toBeNull();
  });

  it('does nothing on confirm with no selection', async () => {
    await setup();
    component.confirmAndReview();
    expect(integrationsSpy.runGitHubReviewPr).not.toHaveBeenCalled();
  });

  it('surfaces an error when starting the review fails', async () => {
    integrationsSpy.runGitHubReviewPr.mockReturnValue(
      throwError(() => ({ error: { detail: 'no such PR' } })),
    );
    await setup();
    component.selectPull(component.pulls[0]);
    component.confirmAndReview();
    expect(component.pullError).toBe('no such PR');
    expect(component.startingReview).toBe(false);
  });

  it('reports terminal status and dismisses the job', async () => {
    await setup();
    expect(component.isJobTerminal()).toBe(false);
    component.jobStatus = { job_id: 'j1', status: 'running' };
    expect(component.isJobTerminal()).toBe(false);
    component.jobStatus = {
      job_id: 'j1',
      status: 'completed',
      github_pr_url: 'https://example.com/pull/1',
      review_summary: { total_issues: 2, inline_comments: 1, body_findings: 1, event: 'REQUEST_CHANGES' },
    };
    expect(component.isJobTerminal()).toBe(true);
    expect(component.jobStatus.review_summary?.event).toBe('REQUEST_CHANGES');
    component.activeJob = { job_id: 'j1', pr_number: 1, pr_url: 'u', status: 'pending', message: '' };
    component.dismissJob();
    expect(component.activeJob).toBeNull();
    expect(component.jobStatus).toBeNull();
  });
});
