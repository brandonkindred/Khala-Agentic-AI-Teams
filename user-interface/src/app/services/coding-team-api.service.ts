import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type { HealthResponse } from '../models/health.model';
import type { CodingTeamJobListItem, CodingTeamJobStatus } from '../models/coding-team.model';
import type { SubmitAnswersRequest } from '../models/software-engineering.model';

/**
 * Coding Team API (Software Engineering sub-team). Base URL from environment.codingTeamApiUrl.
 */
@Injectable({ providedIn: 'root' })
export class CodingTeamApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.codingTeamApiUrl;

  health(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.baseUrl}/health`);
  }

  getJobStatus(jobId: string): Observable<CodingTeamJobStatus> {
    return this.http.get<CodingTeamJobStatus>(`${this.baseUrl}/status/${jobId}`);
  }

  /**
   * Submit answers to a paused job's pending questions.
   *
   * Preconditions: `jobId` identifies a job with `waiting_for_answers: true`;
   * every required pending question has an answer (the backend re-validates
   * fail-closed and returns 400 otherwise).
   * Postconditions: on success the job's pause flag is cleared and the
   * orchestrator resumes; the returned status reflects the post-submit state.
   */
  submitAnswers(jobId: string, request: SubmitAnswersRequest): Observable<CodingTeamJobStatus> {
    return this.http.post<CodingTeamJobStatus>(`${this.baseUrl}/run/${jobId}/answers`, request);
  }

  /**
   * List coding-team jobs. Each item carries `github_context` and
   * `waiting_for_answers` so callers can spot active GitHub-issue runs
   * without per-job status calls.
   *
   * Postconditions: with `activeOnly` (default), only non-terminal jobs are
   * returned and the filtering happens at the job service — terminal jobs'
   * full records never cross the wire.
   */
  listJobs(activeOnly = true): Observable<CodingTeamJobListItem[]> {
    const suffix = activeOnly ? '?active=true' : '';
    return this.http.get<CodingTeamJobListItem[]>(`${this.baseUrl}/jobs${suffix}`);
  }
}
