import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  RunTeamRequest,
  RunTeamResponse,
  JobStatusResponse,
  RetryResponse,
  RePlanWithClarificationsRequest,
  ClarificationCreateRequest,
  ClarificationResponse,
  ClarificationMessageRequest,
  ClarificationSessionResponse,
  ArchitectDesignRequest,
  ArchitectDesignResponse,
  HealthResponse,
  BackendCodeV2RunRequest,
  BackendCodeV2RunResponse,
  BackendCodeV2StatusResponse,
  PlanningV2RunRequest,
  PlanningV2RunResponse,
  PlanningV2StatusResponse,
} from '../models';

/**
 * Service for Software Engineering Team API endpoints.
 * Base URL from environment.softwareEngineeringApiUrl (default port 8000).
 */
@Injectable({ providedIn: 'root' })
export class SoftwareEngineeringApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.softwareEngineeringApiUrl;

  /**
   * POST /run-team
   */
  runTeam(request: RunTeamRequest): Observable<RunTeamResponse> {
    return this.http.post<RunTeamResponse>(
      `${this.baseUrl}/run-team`,
      request
    );
  }

  /**
   * GET /run-team/{job_id}
   */
  getJobStatus(jobId: string): Observable<JobStatusResponse> {
    return this.http.get<JobStatusResponse>(
      `${this.baseUrl}/run-team/${jobId}`
    );
  }

  /**
   * POST /run-team/{job_id}/retry-failed
   */
  retryFailed(jobId: string): Observable<RetryResponse> {
    return this.http.post<RetryResponse>(
      `${this.baseUrl}/run-team/${jobId}/retry-failed`,
      {}
    );
  }

  /**
   * POST /run-team/{job_id}/re-plan-with-clarifications
   */
  rePlanWithClarifications(
    jobId: string,
    request: RePlanWithClarificationsRequest
  ): Observable<RunTeamResponse> {
    return this.http.post<RunTeamResponse>(
      `${this.baseUrl}/run-team/${jobId}/re-plan-with-clarifications`,
      request
    );
  }

  /**
   * POST /clarification/sessions
   */
  createClarificationSession(
    request: ClarificationCreateRequest
  ): Observable<ClarificationResponse> {
    return this.http.post<ClarificationResponse>(
      `${this.baseUrl}/clarification/sessions`,
      request
    );
  }

  /**
   * POST /clarification/sessions/{session_id}/messages
   */
  sendClarificationMessage(
    sessionId: string,
    request: ClarificationMessageRequest
  ): Observable<ClarificationResponse> {
    return this.http.post<ClarificationResponse>(
      `${this.baseUrl}/clarification/sessions/${sessionId}/messages`,
      request
    );
  }

  /**
   * GET /clarification/sessions/{session_id}
   */
  getClarificationSession(
    sessionId: string
  ): Observable<ClarificationSessionResponse> {
    return this.http.get<ClarificationSessionResponse>(
      `${this.baseUrl}/clarification/sessions/${sessionId}`
    );
  }

  /**
   * GET /execution/tasks
   */
  getExecutionTasks(): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(
      `${this.baseUrl}/execution/tasks`
    );
  }

  /**
   * GET /execution/stream - SSE stream.
   * Returns an Observable that emits parsed event data.
   */
  getExecutionStream(): Observable<Record<string, unknown>> {
    return new Observable((subscriber) => {
      const url = `${this.baseUrl}/execution/stream`;
      const eventSource = new EventSource(url);

      eventSource.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data) as Record<string, unknown>;
          subscriber.next(data);
        } catch {
          subscriber.next({ raw: e.data });
        }
      };

      eventSource.onerror = (err) => {
        eventSource.close();
        subscriber.error(err);
      };

      return () => {
        eventSource.close();
      };
    });
  }

  /**
   * POST /architect/design
   */
  architectDesign(
    request: ArchitectDesignRequest
  ): Observable<ArchitectDesignResponse> {
    return this.http.post<ArchitectDesignResponse>(
      `${this.baseUrl}/architect/design`,
      request
    );
  }

  // -----------------------------------------------------------------------
  // Backend-Code-V2
  // -----------------------------------------------------------------------

  /**
   * POST /backend-code-v2/run
   */
  runBackendCodeV2(
    request: BackendCodeV2RunRequest
  ): Observable<BackendCodeV2RunResponse> {
    return this.http.post<BackendCodeV2RunResponse>(
      `${this.baseUrl}/backend-code-v2/run`,
      request
    );
  }

  /**
   * GET /backend-code-v2/status/{job_id}
   */
  getBackendCodeV2Status(
    jobId: string
  ): Observable<BackendCodeV2StatusResponse> {
    return this.http.get<BackendCodeV2StatusResponse>(
      `${this.baseUrl}/backend-code-v2/status/${jobId}`
    );
  }

  // -----------------------------------------------------------------------
  // Planning-V2
  // -----------------------------------------------------------------------

  /**
   * POST /planning-v2/run
   */
  runPlanningV2(
    request: PlanningV2RunRequest
  ): Observable<PlanningV2RunResponse> {
    return this.http.post<PlanningV2RunResponse>(
      `${this.baseUrl}/planning-v2/run`,
      request
    );
  }

  /**
   * GET /planning-v2/status/{job_id}
   */
  getPlanningV2Status(
    jobId: string
  ): Observable<PlanningV2StatusResponse> {
    return this.http.get<PlanningV2StatusResponse>(
      `${this.baseUrl}/planning-v2/status/${jobId}`
    );
  }

  /**
   * GET /health
   */
  health(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.baseUrl}/health`);
  }
}
