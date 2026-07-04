import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import { skipErrorNotify } from '../core/error-handler.interceptor';
import type {
  CodeReviewRunItem,
  GitHubConfigResponse,
  GitHubConfigUpdate,
  GitHubIssueItem,
  GitHubPullRequestItem,
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

  /** GET /api/integrations/github/issues */
  getGitHubIssues(label?: string): Observable<GitHubIssueItem[]> {
    const params: Record<string, string> = {};
    if (label) {
      params['label'] = label;
    }
    return this.http.get<GitHubIssueItem[]>(`${this.baseUrl}/github/issues`, { params });
  }

  /** POST /api/integrations/github/run-issue */
  runGitHubIssue(body: RunGitHubIssueRequest): Observable<RunGitHubIssueResponse> {
    return this.http.post<RunGitHubIssueResponse>(`${this.baseUrl}/github/run-issue`, body);
  }

  /** GET /api/integrations/github/pulls */
  getGitHubPullRequests(): Observable<GitHubPullRequestItem[]> {
    return this.http.get<GitHubPullRequestItem[]>(`${this.baseUrl}/github/pulls`);
  }

  /** POST /api/integrations/github/review-pr */
  runGitHubReviewPr(body: RunPrReviewRequest): Observable<RunPrReviewResponse> {
    return this.http.post<RunPrReviewResponse>(`${this.baseUrl}/github/review-pr`, body);
  }

  /**
   * GET /api/integrations/github/reviews — persisted code-review history for the
   * configured repository (optionally filtered to one PR), newest-first.
   */
  getGitHubReviewHistory(prNumber?: number): Observable<CodeReviewRunItem[]> {
    const params: Record<string, string> = {};
    if (prNumber !== undefined) {
      params['pr_number'] = String(prNumber);
    }
    return this.http.get<CodeReviewRunItem[]>(`${this.baseUrl}/github/reviews`, { params });
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
}
