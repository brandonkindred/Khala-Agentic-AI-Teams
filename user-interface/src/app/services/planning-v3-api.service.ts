import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  PlanningV3RunRequest,
  PlanningV3RunResponse,
  PlanningV3StatusResponse,
  PlanningV3ResultResponse,
  PlanningV3JobsResponse,
} from '../models';
import type { HealthResponse } from '../models/health.model';

/**
 * Service for Planning V3 Team API (client-facing discovery / PRD).
 * Base URL from environment.planningV3ApiUrl.
 */
@Injectable({ providedIn: 'root' })
export class PlanningV3ApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.planningV3ApiUrl;

  /** POST /run */
  run(request: PlanningV3RunRequest): Observable<PlanningV3RunResponse> {
    return this.http.post<PlanningV3RunResponse>(`${this.baseUrl}/run`, request);
  }

  /** GET /status/{job_id} */
  getStatus(jobId: string): Observable<PlanningV3StatusResponse> {
    return this.http.get<PlanningV3StatusResponse>(`${this.baseUrl}/status/${jobId}`);
  }

  /** GET /result/{job_id} */
  getResult(jobId: string): Observable<PlanningV3ResultResponse> {
    return this.http.get<PlanningV3ResultResponse>(`${this.baseUrl}/result/${jobId}`);
  }

  /** GET /jobs */
  getJobs(): Observable<PlanningV3JobsResponse> {
    return this.http.get<PlanningV3JobsResponse>(`${this.baseUrl}/jobs`);
  }

  /** POST /{job_id}/answers - submit answers to open questions */
  submitAnswers(jobId: string, answers: Array<{ question_id: string; selected_option_id?: string; selected_option_ids?: string[]; other_text?: string | null }>): Observable<PlanningV3StatusResponse> {
    return this.http.post<PlanningV3StatusResponse>(`${this.baseUrl}/${jobId}/answers`, { answers });
  }

  /** GET /health */
  health(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.baseUrl}/health`);
  }
}
