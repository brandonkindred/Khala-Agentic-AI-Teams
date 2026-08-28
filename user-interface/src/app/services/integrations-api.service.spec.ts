import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { IntegrationsApiService } from './integrations-api.service';
import { SKIP_ERROR_NOTIFY } from '../core/error-handler.interceptor';
import { environment } from '../../environments/environment';

describe('IntegrationsApiService', () => {
  let service: IntegrationsApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.integrationsApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [IntegrationsApiService],
    });
    service = TestBed.inject(IntegrationsApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should call GET /api/integrations for getIntegrations', () => {
    const mockList = [{ id: 'slack', type: 'slack', enabled: true, channel: '#eng' }];
    service.getIntegrations().subscribe((res) => {
      expect(res).toEqual(mockList);
    });
    const req = httpMock.expectOne(baseUrl);
    expect(req.request.method).toBe('GET');
    req.flush(mockList);
  });

  it('should call GET /api/integrations/slack for getSlackConfig', () => {
    const mockConfig = { enabled: true, webhook_url: null, webhook_configured: true, channel_display_name: '#eng' };
    service.getSlackConfig().subscribe((res) => {
      expect(res).toEqual(mockConfig);
    });
    const req = httpMock.expectOne(`${baseUrl}/slack`);
    expect(req.request.method).toBe('GET');
    req.flush(mockConfig);
  });

  it('should call PUT /api/integrations/slack for updateSlackConfig with body', () => {
    const body = { enabled: true, webhook_url: 'https://hooks.slack.com/x', channel_display_name: '#eng' };
    const mockResponse = { enabled: true, webhook_url: null, webhook_configured: true, channel_display_name: '#eng' };
    service.updateSlackConfig(body).subscribe((res) => {
      expect(res).toEqual(mockResponse);
    });
    const req = httpMock.expectOne(`${baseUrl}/slack`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual(body);
    req.flush(mockResponse);
  });

  it('getSlackOAuthUrl GET', () => {
    service.getSlackOAuthUrl().subscribe();
    httpMock.expectOne(`${baseUrl}/slack/oauth/connect`).flush({});
  });

  it('disconnectSlack DELETE', () => {
    service.disconnectSlack().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/slack/oauth`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('getMediumConfig GET', () => {
    service.getMediumConfig().subscribe();
    httpMock.expectOne(`${baseUrl}/medium`).flush({});
  });

  it('updateMediumConfig PUT', () => {
    service.updateMediumConfig({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/medium`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('importMediumSession POST', () => {
    service.importMediumSession({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/medium/session`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('getGoogleBrowserLoginStatus GET', () => {
    service.getGoogleBrowserLoginStatus().subscribe();
    httpMock.expectOne(`${baseUrl}/google-browser-login`).flush({});
  });

  it('putGoogleBrowserLoginCredentials PUT', () => {
    service.putGoogleBrowserLoginCredentials({} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/google-browser-login`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('deleteGoogleBrowserLoginCredentials DELETE', () => {
    service.deleteGoogleBrowserLoginCredentials().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/google-browser-login`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('mediumBrowserLoginSession POST', () => {
    service.mediumBrowserLoginSession().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/medium/session/browser-login`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('clearMediumSession DELETE', () => {
    service.clearMediumSession().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/medium/session`);
    expect(req.request.method).toBe('DELETE');
    req.flush({});
  });

  it('getGitHubConfig GET', () => {
    const mockConfig = { enabled: true, token_configured: true, default_label: '' };
    service.getGitHubConfig().subscribe((res) => {
      expect(res).toEqual(mockConfig);
    });
    const req = httpMock.expectOne(`${baseUrl}/github`);
    expect(req.request.method).toBe('GET');
    req.flush(mockConfig);
  });

  it('updateGitHubConfig PUT with body (no repository fields — access comes from the PAT)', () => {
    const body = { enabled: true, token: 'ghp_x', default_label: '', repo_path: '', webhook_secret: '' };
    service.updateGitHubConfig(body).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/github`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual(body);
    expect('owner' in (req.request.body as Record<string, unknown>)).toBe(false);
    expect('repo' in (req.request.body as Record<string, unknown>)).toBe(false);
    req.flush({ enabled: true, token_configured: true, default_label: '' });
  });

  it('deleteGitHubConfig DELETE', () => {
    service.deleteGitHubConfig().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/github`);
    expect(req.request.method).toBe('DELETE');
    req.flush({ enabled: false, token_configured: false, default_label: '' });
  });

  it('getGitHubRepos GET emits the typed repo list and sends no query params', () => {
    const repos = [
      {
        owner: 'acme',
        name: 'widget',
        full_name: 'acme/widget',
        private: true,
        archived: false,
        html_url: 'https://github.com/acme/widget',
        description: '',
        default_branch: 'main',
        open_issues_count: 4,
        pushed_at: '2026-07-01T00:00:00Z',
      },
    ];
    let emitted: unknown;
    service.getGitHubRepos().subscribe((res) => {
      emitted = res;
    });
    const req = httpMock.expectOne(`${baseUrl}/github/repos`);
    expect(req.request.method).toBe('GET');
    // The repos endpoint is account-scoped — it must never receive owner/repo params.
    expect(req.request.params.keys().length).toBe(0);
    req.flush(repos);
    expect(emitted).toEqual(repos);
  });

  it('getGitHubRepos does NOT suppress the global error toast (no SKIP_NOTIFY)', () => {
    // Unlike the /github config methods, a repos-list failure must surface through
    // the global toast (the list is a prerequisite for the coding-team/code-review
    // pages), so the request must carry SKIP_ERROR_NOTIFY at its default of false.
    service.getGitHubRepos().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/github/repos`);
    expect(req.request.context.get(SKIP_ERROR_NOTIFY)).toBe(false);
    req.flush([]);
  });

  it('getGitHubConfig DOES suppress the global error toast (SKIP_NOTIFY)', () => {
    // Contrast: the config read renders its own inline error, so it opts out.
    service.getGitHubConfig().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/github`);
    expect(req.request.context.get(SKIP_ERROR_NOTIFY)).toBe(true);
    req.flush({ enabled: false, token_configured: false, default_label: '' });
  });

  it('getGitHubIssues GET with no options sends no params', () => {
    service.getGitHubIssues().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/github/issues`);
    expect(req.request.method).toBe('GET');
    expect(req.request.params.keys().length).toBe(0);
    req.flush([]);
  });

  it('getGitHubIssues GET with an explicit undefined label sends no label param', () => {
    // Mirrors what the coding-team page sends when its default-label filter is off/unset:
    // `label: undefined` must not become a `label` query param (would filter to nothing).
    service.getGitHubIssues({ owner: 'acme', repo: 'widget', label: undefined }).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/github/issues`);
    expect(req.request.params.has('label')).toBe(false);
    expect(req.request.params.get('owner')).toBe('acme');
    expect(req.request.params.get('repo')).toBe('widget');
    req.flush([]);
  });

  it('getGitHubIssues GET with only a label param', () => {
    service.getGitHubIssues({ label: 'bug' }).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/github/issues`);
    expect(req.request.params.get('label')).toBe('bug');
    expect(req.request.params.has('owner')).toBe(false);
    expect(req.request.params.has('repo')).toBe(false);
    req.flush([]);
  });

  it('getGitHubIssues GET with owner/repo params', () => {
    service.getGitHubIssues({ owner: 'acme', repo: 'widget' }).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/github/issues`);
    expect(req.request.method).toBe('GET');
    expect(req.request.params.get('owner')).toBe('acme');
    expect(req.request.params.get('repo')).toBe('widget');
    req.flush([]);
  });

  it('getGitHubIssues GET passes a partial owner-only pair through (backend returns 400)', () => {
    service.getGitHubIssues({ owner: 'acme' }).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/github/issues`);
    // The lone param is sent so the backend rejects it, rather than being silently dropped.
    expect(req.request.params.get('owner')).toBe('acme');
    expect(req.request.params.has('repo')).toBe(false);
    req.flush([]);
  });

  it('getGitHubPullRequests GET passes a partial repo-only pair through', () => {
    service.getGitHubPullRequests({ repo: 'widget' }).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/github/pulls`);
    expect(req.request.params.get('repo')).toBe('widget');
    expect(req.request.params.has('owner')).toBe(false);
    req.flush([]);
  });

  it('getGitHubReviewHistory GET passes a partial owner-only pair through', () => {
    service.getGitHubReviewHistory({ owner: 'acme' }).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/github/reviews`);
    expect(req.request.params.get('owner')).toBe('acme');
    expect(req.request.params.has('repo')).toBe(false);
    req.flush([]);
  });

  it('runGitHubIssue POST with body', () => {
    service.runGitHubIssue({ issue_number: 7, owner: 'acme', repo: 'widget' }).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/github/run-issue`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ issue_number: 7, owner: 'acme', repo: 'widget' });
    req.flush({ job_id: 'j1', issue_number: 7, issue_url: 'u', status: 'pending', message: '' });
  });

  it('getGitHubPullRequests GET with no options sends no params', () => {
    service.getGitHubPullRequests().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/github/pulls`);
    expect(req.request.method).toBe('GET');
    expect(req.request.params.keys().length).toBe(0);
    req.flush([]);
  });

  it('getGitHubPullRequests GET with owner/repo params', () => {
    service.getGitHubPullRequests({ owner: 'acme', repo: 'widget' }).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/github/pulls`);
    expect(req.request.method).toBe('GET');
    expect(req.request.params.get('owner')).toBe('acme');
    expect(req.request.params.get('repo')).toBe('widget');
    req.flush([]);
  });

  it('runGitHubReviewPr POST with body', () => {
    service.runGitHubReviewPr({ pr_number: 7, owner: 'acme', repo: 'widget' }).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/github/review-pr`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ pr_number: 7, owner: 'acme', repo: 'widget' });
    req.flush({ job_id: 'j1', pr_number: 7, pr_url: 'u', status: 'pending', message: '' });
  });

  it('addressPrComments POST hits the PR-scoped path with the owner/repo body', () => {
    service.addressPrComments(7, { owner: 'acme', repo: 'widget' }).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/github/pulls/7/address-comments`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ owner: 'acme', repo: 'widget' });
    req.flush({
      job_id: 'a1',
      pr_number: 7,
      pr_url: 'u',
      unresolved_comment_count: 3,
      status: 'pending',
      message: '',
    });
  });

  it('addressPrComments POST defaults to an empty body', () => {
    service.addressPrComments(9).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/github/pulls/9/address-comments`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({});
    req.flush({
      job_id: 'a2',
      pr_number: 9,
      pr_url: 'u',
      unresolved_comment_count: 0,
      status: 'pending',
      message: '',
    });
  });

  it('getGitHubReviewHistory GET with no options sends no params', () => {
    service.getGitHubReviewHistory().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/github/reviews`);
    expect(req.request.method).toBe('GET');
    expect(req.request.params.keys().length).toBe(0);
    req.flush([]);
  });

  it('getGitHubReviewHistory GET with pr_number param', () => {
    service.getGitHubReviewHistory({ prNumber: 7 }).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/github/reviews`);
    expect(req.request.method).toBe('GET');
    expect(req.request.params.get('pr_number')).toBe('7');
    req.flush([]);
  });

  it('getGitHubReviewHistory GET with owner/repo params', () => {
    service.getGitHubReviewHistory({ owner: 'acme', repo: 'widget' }).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/github/reviews`);
    expect(req.request.method).toBe('GET');
    expect(req.request.params.has('pr_number')).toBe(false);
    expect(req.request.params.get('owner')).toBe('acme');
    expect(req.request.params.get('repo')).toBe('widget');
    req.flush([]);
  });

  it('getGitHubReviewHistory GET with pr_number AND owner/repo params', () => {
    service.getGitHubReviewHistory({ prNumber: 42, owner: 'acme', repo: 'widget' }).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/github/reviews`);
    expect(req.request.params.get('pr_number')).toBe('42');
    expect(req.request.params.get('owner')).toBe('acme');
    expect(req.request.params.get('repo')).toBe('widget');
    req.flush([]);
  });

  it('createGitHubReviewIssues POST with owner/repo, job id + proposal ids', () => {
    service.createGitHubReviewIssues('acme', 'widget', 'rev 9', ['p0', 'p1']).subscribe();
    // The job id is URL-encoded into the path; owner/repo ride in the body.
    const req = httpMock.expectOne(`${baseUrl}/github/reviews/rev%209/issues`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ proposal_ids: ['p0', 'p1'], owner: 'acme', repo: 'widget' });
    req.flush({ job_id: 'rev 9', created: [], proposals: [] });
  });

  it('getGitHubReviewTranscript GET with owner/repo params and encoded job id', () => {
    const entries = [
      {
        stage: 'chunk_review',
        target: 'a.py',
        model: 'm',
        prompt: 'p',
        response: 'r',
        started_at: '2024-01-01T00:00:00Z',
        duration_ms: 10,
      },
    ];
    service.getGitHubReviewTranscript('acme', 'widget', 'rev 9').subscribe((res) => {
      expect(res).toEqual({ job_id: 'rev 9', entries });
    });
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/github/reviews/rev%209/transcript`);
    expect(req.request.method).toBe('GET');
    expect(req.request.params.get('owner')).toBe('acme');
    expect(req.request.params.get('repo')).toBe('widget');
    req.flush({ job_id: 'rev 9', entries });
  });

  it('getTradingViewConfig GET', () => {
    const mockConfig = {
      enabled: true,
      mcp_server_url: 'https://tv/mcp',
      tool_name: 'get_ohlcv',
      auth_token_configured: true,
    };
    service.getTradingViewConfig().subscribe((res) => {
      expect(res).toEqual(mockConfig);
    });
    const req = httpMock.expectOne(`${baseUrl}/tradingview`);
    expect(req.request.method).toBe('GET');
    req.flush(mockConfig);
  });

  it('updateTradingViewConfig PUT with body', () => {
    const body = {
      enabled: true,
      mcp_server_url: 'https://tv/mcp',
      tool_name: '',
      auth_token: 'secret',
    };
    service.updateTradingViewConfig(body).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/tradingview`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual(body);
    req.flush({
      enabled: true,
      mcp_server_url: 'https://tv/mcp',
      tool_name: 'get_ohlcv',
      auth_token_configured: true,
    });
  });

  it('deleteTradingViewConfig DELETE', () => {
    service.deleteTradingViewConfig().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/tradingview`);
    expect(req.request.method).toBe('DELETE');
    req.flush({ enabled: false, mcp_server_url: '', tool_name: '', auth_token_configured: false });
  });

  it('testTradingViewConnection POST', () => {
    let result: { ok: boolean; detail: string } | undefined;
    service.testTradingViewConnection().subscribe((res) => {
      result = res;
    });
    const req = httpMock.expectOne(`${baseUrl}/tradingview/test`);
    expect(req.request.method).toBe('POST');
    req.flush({ ok: true, detail: 'Connected — 5 bars.' });
    expect(result).toEqual({ ok: true, detail: 'Connected — 5 bars.' });
  });
});
