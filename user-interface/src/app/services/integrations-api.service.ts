import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import { skipErrorNotify } from '../core/error-handler.interceptor';
import type {
  CodeReviewRunItem,
  CodeReviewTranscript,
  CreateReviewIssuesRequest,
  CreateReviewIssuesResponse,
  GitHubConfigResponse,
  GitHubConfigUpdate,
  GitHubIssueItem,
  GitHubPullRequestItem,
  GitHubRepoItem,
  GoogleBrowserLoginCredentialsBody,
  GoogleBrowserLoginStatusResponse,
  IntegrationListItem,
  MediumConfigResponse,
  MediumConfigUpdate,
  MediumSessionImportBody,
  RunGitHubIssueRequest,
  RunGitHubIssueResponse,
  RunPrReviewRequest,
  RunPrReviewResponse,
  SlackConfigResponse,
  SlackConfigUpdate,
  SlackOAuthConnectResponse,
  TradingViewConfigResponse,
  TradingViewConfigUpdate,
  TradingViewTestResponse,
} from '../models/integrations.model';

/**
 * Service for Integrations API (Slack config, OAuth, etc.).
 * Base URL from environment.integrationsApiUrl.
 *
 * The configuration CRUD calls (Slack/Medium/Google/GitHub config) opt out of
 * the global error toast (`SKIP_NOTIFY`): the integrations dashboard renders its
 * own inline error banner, and the coding-team page's passive GitHub-config
 * probe swallows failures. The GitHub issue/PR/review action calls keep the
 * global toast, since their callers do not surface errors themselves.
 */
@Injectable({ providedIn: 'root' })
export class IntegrationsApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.integrationsApiUrl;

  /** Request options that suppress the global error toast (the interceptor only reads this). */
  private readonly SKIP_NOTIFY = { context: skipErrorNotify() };

  /** GET /api/integrations - list integrations. */
  getIntegrations(): Observable<IntegrationListItem[]> {
    return this.http.get<IntegrationListItem[]>(this.baseUrl, this.SKIP_NOTIFY);
  }

  /** GET /api/integrations/slack - get Slack config. */
  getSlackConfig(): Observable<SlackConfigResponse> {
    return this.http.get<SlackConfigResponse>(`${this.baseUrl}/slack`, this.SKIP_NOTIFY);
  }

  /** PUT /api/integrations/slack - update Slack config (manual / webhook mode). */
  updateSlackConfig(body: SlackConfigUpdate): Observable<SlackConfigResponse> {
    return this.http.put<SlackConfigResponse>(`${this.baseUrl}/slack`, body, this.SKIP_NOTIFY);
  }

  /**
   * GET /api/integrations/slack/oauth/connect
   * Returns the Slack OAuth authorization URL to redirect the user to.
   */
  getSlackOAuthUrl(): Observable<SlackOAuthConnectResponse> {
    return this.http.get<SlackOAuthConnectResponse>(`${this.baseUrl}/slack/oauth/connect`, this.SKIP_NOTIFY);
  }

  /**
   * DELETE /api/integrations/slack/oauth
   * Disconnects the Slack OAuth connection (removes stored token and team info).
   */
  disconnectSlack(): Observable<SlackConfigResponse> {
    return this.http.delete<SlackConfigResponse>(`${this.baseUrl}/slack/oauth`, this.SKIP_NOTIFY);
  }

  /** GET /api/integrations/medium */
  getMediumConfig(): Observable<MediumConfigResponse> {
    return this.http.get<MediumConfigResponse>(`${this.baseUrl}/medium`, this.SKIP_NOTIFY);
  }

  /** PUT /api/integrations/medium */
  updateMediumConfig(body: MediumConfigUpdate): Observable<MediumConfigResponse> {
    return this.http.put<MediumConfigResponse>(`${this.baseUrl}/medium`, body, this.SKIP_NOTIFY);
  }

  /** POST /api/integrations/medium/session */
  importMediumSession(body: MediumSessionImportBody): Observable<MediumConfigResponse> {
    return this.http.post<MediumConfigResponse>(`${this.baseUrl}/medium/session`, body, this.SKIP_NOTIFY);
  }

  /** GET /api/integrations/google-browser-login */
  getGoogleBrowserLoginStatus(): Observable<GoogleBrowserLoginStatusResponse> {
    return this.http.get<GoogleBrowserLoginStatusResponse>(`${this.baseUrl}/google-browser-login`, this.SKIP_NOTIFY);
  }

  /** PUT /api/integrations/google-browser-login */
  putGoogleBrowserLoginCredentials(
    body: GoogleBrowserLoginCredentialsBody,
  ): Observable<GoogleBrowserLoginStatusResponse> {
    return this.http.put<GoogleBrowserLoginStatusResponse>(
      `${this.baseUrl}/google-browser-login`,
      body,
      this.SKIP_NOTIFY,
    );
  }

  /** DELETE /api/integrations/google-browser-login */
  deleteGoogleBrowserLoginCredentials(): Observable<GoogleBrowserLoginStatusResponse> {
    return this.http.delete<GoogleBrowserLoginStatusResponse>(`${this.baseUrl}/google-browser-login`, this.SKIP_NOTIFY);
  }

  /** POST /api/integrations/medium/session/browser-login — uses stored encrypted credentials. */
  mediumBrowserLoginSession(): Observable<MediumConfigResponse> {
    return this.http.post<MediumConfigResponse>(
      `${this.baseUrl}/medium/session/browser-login`,
      {},
      this.SKIP_NOTIFY,
    );
  }

  /** DELETE /api/integrations/medium/session */
  clearMediumSession(): Observable<MediumConfigResponse> {
    return this.http.delete<MediumConfigResponse>(`${this.baseUrl}/medium/session`, this.SKIP_NOTIFY);
  }

  /** GET /api/integrations/github */
  getGitHubConfig(): Observable<GitHubConfigResponse> {
    return this.http.get<GitHubConfigResponse>(`${this.baseUrl}/github`, this.SKIP_NOTIFY);
  }

  /** PUT /api/integrations/github */
  updateGitHubConfig(body: GitHubConfigUpdate): Observable<GitHubConfigResponse> {
    return this.http.put<GitHubConfigResponse>(`${this.baseUrl}/github`, body, this.SKIP_NOTIFY);
  }

  /** DELETE /api/integrations/github */
  deleteGitHubConfig(): Observable<GitHubConfigResponse> {
    return this.http.delete<GitHubConfigResponse>(`${this.baseUrl}/github`, this.SKIP_NOTIFY);
  }

  /**
   * GET /api/integrations/github/repos — every repository the stored PAT can
   * access (the token's own authorization configuration is the source of truth).
   *
   * Unlike the `/github` config methods above, this deliberately omits
   * `SKIP_NOTIFY`: the repo list is a prerequisite for the coding-team and
   * code-review pages, so a failure should surface through the global error
   * toast rather than fail silently and leave those pages ambiguously empty.
   */
  getGitHubRepos(): Observable<GitHubRepoItem[]> {
    return this.http.get<GitHubRepoItem[]>(`${this.baseUrl}/github/repos`);
  }

  /**
   * GET /api/integrations/github/issues — open issues for one accessible repo.
   * Omitting `owner`/`repo` falls back to the backend's legacy configured default.
   */
  getGitHubIssues(options: { label?: string; owner?: string; repo?: string } = {}): Observable<GitHubIssueItem[]> {
    const params: Record<string, string> = {};
    if (options.label) {
      params['label'] = options.label;
    }
    // Send whichever of owner/repo is provided rather than dropping both when only one is
    // set — a partial pair is a caller error the backend rejects with a 400, and silently
    // falling back to the configured default would mask it.
    if (options.owner) {
      params['owner'] = options.owner;
    }
    if (options.repo) {
      params['repo'] = options.repo;
    }
    return this.http.get<GitHubIssueItem[]>(`${this.baseUrl}/github/issues`, { params });
  }

  /** POST /api/integrations/github/run-issue */
  runGitHubIssue(body: RunGitHubIssueRequest): Observable<RunGitHubIssueResponse> {
    return this.http.post<RunGitHubIssueResponse>(`${this.baseUrl}/github/run-issue`, body);
  }

  /**
   * GET /api/integrations/github/pulls — open PRs for one accessible repo.
   * Omitting `owner`/`repo` falls back to the backend's legacy configured default.
   */
  getGitHubPullRequests(options: { owner?: string; repo?: string } = {}): Observable<GitHubPullRequestItem[]> {
    const params: Record<string, string> = {};
    // Pass through a partial pair so the backend's 400 surfaces (see getGitHubIssues).
    if (options.owner) {
      params['owner'] = options.owner;
    }
    if (options.repo) {
      params['repo'] = options.repo;
    }
    return this.http.get<GitHubPullRequestItem[]>(`${this.baseUrl}/github/pulls`, { params });
  }

  /** POST /api/integrations/github/review-pr */
  runGitHubReviewPr(body: RunPrReviewRequest): Observable<RunPrReviewResponse> {
    return this.http.post<RunPrReviewResponse>(`${this.baseUrl}/github/review-pr`, body);
  }

  /**
   * GET /api/integrations/github/reviews — persisted code-review history for one
   * accessible repository (optionally filtered to one PR), newest-first.
   * Omitting `owner`/`repo` falls back to the backend's legacy configured default.
   */
  getGitHubReviewHistory(
    options: { prNumber?: number; owner?: string; repo?: string } = {},
  ): Observable<CodeReviewRunItem[]> {
    const params: Record<string, string> = {};
    if (options.prNumber !== undefined) {
      params['pr_number'] = String(options.prNumber);
    }
    // Pass through a partial pair so the backend's 400 surfaces (see getGitHubIssues).
    if (options.owner) {
      params['owner'] = options.owner;
    }
    if (options.repo) {
      params['repo'] = options.repo;
    }
    return this.http.get<CodeReviewRunItem[]>(`${this.baseUrl}/github/reviews`, { params });
  }

  /**
   * POST /api/integrations/github/reviews/{jobId}/issues — file GitHub issues for
   * the selected pre-existing findings of a completed review. ``owner``/``repo``
   * name the repository the review belongs to (validated server-side against the
   * review). Returns the created issues plus the review's updated proposal list.
   */
  createGitHubReviewIssues(
    owner: string,
    repo: string,
    jobId: string,
    proposalIds: string[],
  ): Observable<CreateReviewIssuesResponse> {
    const body: CreateReviewIssuesRequest = { proposal_ids: proposalIds, owner, repo };
    return this.http.post<CreateReviewIssuesResponse>(
      `${this.baseUrl}/github/reviews/${encodeURIComponent(jobId)}/issues`,
      body,
    );
  }

  /**
   * GET /api/integrations/github/reviews/{jobId}/transcript — a completed review's
   * full durable transcript (every LLM call the pipeline made, in call order).
   * ``owner``/``repo`` name the repository the review belongs to (validated
   * server-side, same gate as ``getGitHubReviewHistory``/``createGitHubReviewIssues``).
   */
  getGitHubReviewTranscript(owner: string, repo: string, jobId: string): Observable<CodeReviewTranscript> {
    return this.http.get<CodeReviewTranscript>(
      `${this.baseUrl}/github/reviews/${encodeURIComponent(jobId)}/transcript`,
      { params: { owner, repo } },
    );
  }

  /** GET /api/integrations/tradingview */
  getTradingViewConfig(): Observable<TradingViewConfigResponse> {
    return this.http.get<TradingViewConfigResponse>(`${this.baseUrl}/tradingview`, this.SKIP_NOTIFY);
  }

  /** PUT /api/integrations/tradingview */
  updateTradingViewConfig(body: TradingViewConfigUpdate): Observable<TradingViewConfigResponse> {
    return this.http.put<TradingViewConfigResponse>(`${this.baseUrl}/tradingview`, body, this.SKIP_NOTIFY);
  }

  /** DELETE /api/integrations/tradingview */
  deleteTradingViewConfig(): Observable<TradingViewConfigResponse> {
    return this.http.delete<TradingViewConfigResponse>(`${this.baseUrl}/tradingview`, this.SKIP_NOTIFY);
  }

  /** POST /api/integrations/tradingview/test — probe the stored MCP server for reachability. */
  testTradingViewConnection(): Observable<TradingViewTestResponse> {
    return this.http.post<TradingViewTestResponse>(`${this.baseUrl}/tradingview/test`, {}, this.SKIP_NOTIFY);
  }
}
