import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  LlmProviderCreate,
  LlmProviderListResponse,
  LlmProviderUpdate,
} from '../models/llm-config.model';

/**
 * Service for the LLM Provider list API (`/api/llm-config/providers`). The ordered
 * provider list is the sole source of LLM configuration. Base URL from
 * `environment.llmConfigApiUrl`.
 */
@Injectable({ providedIn: 'root' })
export class LlmConfigApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.llmConfigApiUrl;

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
