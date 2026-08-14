import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import { skipErrorNotify } from '../core/error-handler.interceptor';
import type { LlmUsageCall, LlmUsageSummary, LlmUsageWindow } from '../models/llm-usage.model';

@Injectable({ providedIn: 'root' })
export class LlmUsageApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.llmUsageApiUrl;
  private readonly SKIP_NOTIFY = { context: skipErrorNotify() };

  getSummary(window: LlmUsageWindow): Observable<LlmUsageSummary> {
    const params = new HttpParams().set('window', window);
    return this.http.get<LlmUsageSummary>(`${this.baseUrl}/`, { ...this.SKIP_NOTIFY, params });
  }

  getRecent(window: LlmUsageWindow, limit = 100): Observable<LlmUsageCall[]> {
    const params = new HttpParams().set('window', window).set('limit', String(limit));
    return this.http.get<LlmUsageCall[]>(`${this.baseUrl}/recent`, { ...this.SKIP_NOTIFY, params });
  }
}
