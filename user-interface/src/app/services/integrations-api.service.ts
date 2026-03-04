import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  IntegrationListItem,
  SlackConfigResponse,
  SlackConfigUpdate,
} from '../models/integrations.model';

/**
 * Service for Integrations API (Slack config, etc.).
 * Base URL from environment.integrationsApiUrl.
 */
@Injectable({ providedIn: 'root' })
export class IntegrationsApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.integrationsApiUrl;

  /**
   * GET /api/integrations - list integrations.
   */
  getIntegrations(): Observable<IntegrationListItem[]> {
    return this.http.get<IntegrationListItem[]>(this.baseUrl);
  }

  /**
   * GET /api/integrations/slack - get Slack config.
   */
  getSlackConfig(): Observable<SlackConfigResponse> {
    return this.http.get<SlackConfigResponse>(`${this.baseUrl}/slack`);
  }

  /**
   * PUT /api/integrations/slack - update Slack config.
   */
  updateSlackConfig(body: SlackConfigUpdate): Observable<SlackConfigResponse> {
    return this.http.put<SlackConfigResponse>(`${this.baseUrl}/slack`, body);
  }
}
