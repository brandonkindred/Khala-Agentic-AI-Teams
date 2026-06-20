import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type { SeMetrics } from '../models/se-metrics.model';

/**
 * Service for the Software Engineering DORA metrics endpoint.
 * Base URL from environment.softwareEngineeringApiUrl; hits `/dora`.
 */
@Injectable({ providedIn: 'root' })
export class SeMetricsApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.softwareEngineeringApiUrl;

  /** GET /dora?window_days=N (DORA metrics + cost; path avoids "metrics" so it stays traced) */
  getMetrics(windowDays: number): Observable<SeMetrics> {
    const params = new HttpParams().set('window_days', String(windowDays));
    return this.http.get<SeMetrics>(`${this.baseUrl}/dora`, { params });
  }
}
