import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type { LlmConfigResponse, LlmConfigUpdate } from '../models/llm-config.model';

/**
 * Service for the LLM Provider settings API (`/api/llm-config`).
 * Base URL from `environment.llmConfigApiUrl`.
 */
@Injectable({ providedIn: 'root' })
export class LlmConfigApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.llmConfigApiUrl;

  /** GET /api/llm-config — current effective provider config (keys masked). */
  getConfig(): Observable<LlmConfigResponse> {
    return this.http.get<LlmConfigResponse>(this.baseUrl);
  }

  /** PUT /api/llm-config — persist provider/model/keys (empty fields unchanged). */
  updateConfig(body: LlmConfigUpdate): Observable<LlmConfigResponse> {
    return this.http.put<LlmConfigResponse>(this.baseUrl, body);
  }
}
