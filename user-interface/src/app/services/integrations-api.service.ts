import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  GitHubConfigResponse,
  GitHubConfigUpdate,
  GitHubIssueItem,
  GoogleBrowserLoginCredentialsBody,
  GoogleBrowserLoginStatusResponse,
  IntegrationListItem,
  MediumConfigResponse,
  MediumConfigUpdate,
  MediumSessionImportBody,
  RunGitHubIssueRequest,
  RunGitHubIssueResponse,
  SlackConfigResponse,
  SlackConfigUpdate,
  SlackOAuthConnectResponse,
} from '../models/integrations.model';

/**
 * Service for Integrations API (Slack config, OAuth, etc.).
 * Base URL from environment.integrationsApiUrl.
 */
@Injectable({ providedIn: 'root' })
export class IntegrationsApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.integrationsApiUrl;

  /** GET /api/integrations - list integrations. */
  getIntegrations(): Observable<IntegrationListItem[]> {
    return this.http.get<IntegrationListItem[]>(this.baseUrl);
  }

  /** GET /api/integrations/slack - get Slack config. */
  getSlackConfig(): Observable<SlackConfigResponse> {
    return this.http.get<SlackConfigResponse>(`${this.baseUrl}/slack`);
  }

  /** PUT /api/integrations/slack - update Slack config (manual / webhook mode). */
  updateSlackConfig(body: SlackConfigUpdate): Observable<SlackConfigResponse> {
    return this.http.put<SlackConfigResponse>(`${this.baseUrl}/slack`, body);
  }

  /**
   * GET /api/integrations/slack/oauth/connect
   * Returns the Slack OAuth authorization URL to redirect the user to.
   */
  getSlackOAuthUrl(): Observable<SlackOAuthConnectResponse> {
    return this.http.get<SlackOAuthConnectResponse>(`${this.baseUrl}/slack/oauth/connect`);
  }

  /**
   * DELETE /api/integrations/slack/oauth
   * Disconnects the Slack OAuth connection (removes stored token and team info).
   */
  disconnectSlack(): Observable<SlackConfigResponse> {
    return this.http.delete<SlackConfigResponse>(`${this.baseUrl}/slack/oauth`);
  }

  /** GET /api/integrations/medium */
  getMediumConfig(): Observable<MediumConfigResponse> {
    return this.http.get<MediumConfigResponse>(`${this.baseUrl}/medium`);
  }

  /** PUT /api/integrations/medium */
  updateMediumConfig(body: MediumConfigUpdate): Observable<MediumConfigResponse> {
    return this.http.put<MediumConfigResponse>(`${this.baseUrl}/medium`, body);
  }

  /** POST /api/integrations/medium/session */
  importMediumSession(body: MediumSessionImportBody): Observable<MediumConfigResponse> {
    return this.http.post<MediumConfigResponse>(`${this.baseUrl}/medium/session`, body);
  }

  /** GET /api/integrations/google-browser-login */
  getGoogleBrowserLoginStatus(): Observable<GoogleBrowserLoginStatusResponse> {
    return this.http.get<GoogleBrowserLoginStatusResponse>(`${this.baseUrl}/google-browser-login`);
  }

  /** PUT /api/integrations/google-browser-login */
  putGoogleBrowserLoginCredentials(
    body: GoogleBrowserLoginCredentialsBody,
  ): Observable<GoogleBrowserLoginStatusResponse> {
    return this.http.put<GoogleBrowserLoginStatusResponse>(`${this.baseUrl}/google-browser-login`, body);
  }

  /** DELETE /api/integrations/google-browser-login */
  deleteGoogleBrowserLoginCredentials(): Observable<GoogleBrowserLoginStatusResponse> {
    return this.http.delete<GoogleBrowserLoginStatusResponse>(`${this.baseUrl}/google-browser-login`);
  }

  /** POST /api/integrations/medium/session/browser-login — uses stored encrypted credentials. */
  mediumBrowserLoginSession(): Observable<MediumConfigResponse> {
    return this.http.post<MediumConfigResponse>(`${this.baseUrl}/medium/session/browser-login`, {});
  }

  /** DELETE /api/integrations/medium/session */
  clearMediumSession(): Observable<MediumConfigResponse> {
    return this.http.delete<MediumConfigResponse>(`${this.baseUrl}/medium/session`);
  }

  /** GET /api/integrations/github */
  getGitHubConfig(): Observable<GitHubConfigResponse> {
    return this.http.get<GitHubConfigResponse>(`${this.baseUrl}/github`);
  }

  /** PUT /api/integrations/github */
  updateGitHubConfig(body: GitHubConfigUpdate): Observable<GitHubConfigResponse> {
    return this.http.put<GitHubConfigResponse>(`${this.baseUrl}/github`, body);
  }

  /** DELETE /api/integrations/github */
  deleteGitHubConfig(): Observable<GitHubConfigResponse> {
    return this.http.delete<GitHubConfigResponse>(`${this.baseUrl}/github`);
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
}
