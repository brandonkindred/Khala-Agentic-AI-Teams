import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  PlanningRunRequest,
  PlanningRunResponse,
  PlanningStatusResponse,
  PlanningResultResponse,
  PlanningJobsResponse,
} from '../models';
import type { HealthResponse } from '../models/health.model';

/** A single answer to an open planning question, posted to `/{job_id}/answers`. */
export interface PlanningAnswerSubmission {
  question_id: string;
  selected_option_id?: string;
  selected_option_ids?: string[];
  other_text?: string | null;
}

/**
 * Service for Planning Team API (client-facing discovery / PRD).
 * Base URL from environment.planningApiUrl.
 */
@Injectable({ providedIn: 'root' })
export class PlanningApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.planningApiUrl;

  /** POST /run */
  run(request: PlanningRunRequest): Observable<PlanningRunResponse> {
    return this.http.post<PlanningRunResponse>(`${this.baseUrl}/run`, request);
  }

  /** GET /status/{job_id} */
  getStatus(jobId: string): Observable<PlanningStatusResponse> {
    return this.http.get<PlanningStatusResponse>(`${this.baseUrl}/status/${jobId}`);
  }

  /** GET /result/{job_id} */
  getResult(jobId: string): Observable<PlanningResultResponse> {
    return this.http.get<PlanningResultResponse>(`${this.baseUrl}/result/${jobId}`);
  }

  /** GET /jobs */
  getJobs(): Observable<PlanningJobsResponse> {
    return this.http.get<PlanningJobsResponse>(`${this.baseUrl}/jobs`);
  }

  /** POST /{job_id}/answers - submit answers to open questions */
  submitAnswers(jobId: string, answers: PlanningAnswerSubmission[]): Observable<PlanningStatusResponse> {
    return this.http.post<PlanningStatusResponse>(`${this.baseUrl}/${jobId}/answers`, { answers });
  }

  /** GET /health */
  health(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.baseUrl}/health`);
  }
}
