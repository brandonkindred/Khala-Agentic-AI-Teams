import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  LlmConfigResponse,
  LlmConfigUpdate,
  LlmProviderCreate,
  LlmProviderListResponse,
  LlmProviderUpdate,
  OllamaModelsResponse,
} from '../models/llm-config.model';

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

  /**
   * GET /api/llm-config/ollama-models — live model list from the effective Ollama
   * endpoint (`/api/tags`), or the curated fallback when it can't be reached.
   */
  getOllamaModels(): Observable<OllamaModelsResponse> {
    return this.http.get<OllamaModelsResponse>(`${this.baseUrl}/ollama-models`);
  }

  /** GET /api/llm-config/providers — the ordered fallback list (keys masked). */
  listProviders(): Observable<LlmProviderListResponse> {
    return this.http.get<LlmProviderListResponse>(`${this.baseUrl}/providers`);
  }

  /** POST /api/llm-config/providers — append a provider to the list. */
  createProvider(body: LlmProviderCreate): Observable<LlmProviderListResponse> {
    return this.http.post<LlmProviderListResponse>(`${this.baseUrl}/providers`, body);
  }

  /** PUT /api/llm-config/providers/{id} — edit a provider (empty fields unchanged). */
  updateProvider(id: number, body: LlmProviderUpdate): Observable<LlmProviderListResponse> {
    return this.http.put<LlmProviderListResponse>(`${this.baseUrl}/providers/${id}`, body);
  }

  /** DELETE /api/llm-config/providers/{id} — remove a provider from the list. */
  deleteProvider(id: number): Observable<LlmProviderListResponse> {
    return this.http.delete<LlmProviderListResponse>(`${this.baseUrl}/providers/${id}`);
  }

  /** PUT /api/llm-config/providers/order — reorder the list (ids, most→least preferred). */
  reorderProviders(ids: number[]): Observable<LlmProviderListResponse> {
    return this.http.put<LlmProviderListResponse>(`${this.baseUrl}/providers/order`, { ids });
  }
}
