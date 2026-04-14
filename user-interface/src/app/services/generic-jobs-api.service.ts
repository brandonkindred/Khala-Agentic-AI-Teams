/**
 * Generic job management service that proxies to the unified API's
 * /api/jobs/{team}/{jobId} endpoints.  Works for any team — used as
 * a universal fallback when a team doesn't expose its own lifecycle
 * endpoints (cancel, delete, etc.).
 */
import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

const API_BASE = 'http://localhost:8888';

export interface GenericJobRecord {
  job_id: string;
  team: string;
  status: string;
  data: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export interface GenericJobListResponse {
  jobs: GenericJobRecord[];
}

@Injectable({ providedIn: 'root' })
export class GenericJobsApiService {
  private readonly http = inject(HttpClient);

  listJobs(team: string, runningOnly = false): Observable<GenericJobListResponse> {
    const params: Record<string, string> = {};
    if (runningOnly) params['running_only'] = 'true';
    return this.http.get<GenericJobListResponse>(
      `${API_BASE}/api/jobs/${encodeURIComponent(team)}`,
      { params }
    );
  }

  cancel(team: string, jobId: string): Observable<unknown> {
    return this.http.post(`${API_BASE}/api/jobs/${team}/${encodeURIComponent(jobId)}/cancel`, {});
  }

  resume(team: string, jobId: string): Observable<unknown> {
    return this.http.post(`${API_BASE}/api/jobs/${team}/${encodeURIComponent(jobId)}/resume`, {});
  }

  restart(team: string, jobId: string): Observable<unknown> {
    return this.http.post(`${API_BASE}/api/jobs/${team}/${encodeURIComponent(jobId)}/restart`, {});
  }

  interrupt(team: string, jobId: string): Observable<unknown> {
    return this.http.post(`${API_BASE}/api/jobs/${team}/${encodeURIComponent(jobId)}/interrupt`, {});
  }

  delete(team: string, jobId: string): Observable<unknown> {
    return this.http.delete(`${API_BASE}/api/jobs/${team}/${encodeURIComponent(jobId)}`);
  }

  markAllInterrupted(team: string): Observable<unknown> {
    return this.http.post(`${API_BASE}/api/jobs/${team}/mark-all-interrupted`, {});
  }
}
