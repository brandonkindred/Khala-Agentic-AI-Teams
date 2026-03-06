import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  RunTeamRequest,
  RunTeamResponse,
  JobStatusResponse,
  RunningJobsResponse,
  RetryResponse,
  SubmitAnswersRequest,
  ArchitectDesignRequest,
  ArchitectDesignResponse,
  HealthResponse,
  BackendCodeV2RunRequest,
  BackendCodeV2RunResponse,
  BackendCodeV2StatusResponse,
  FrontendCodeV2RunRequest,
  FrontendCodeV2RunResponse,
  FrontendCodeV2StatusResponse,
  PlanningV2RunRequest,
  PlanningV2RunResponse,
  PlanningV2StatusResponse,
  PlanningV2ResultResponse,
  AutoAnswerRequest,
  AutoAnswerResponse,
  ProductAnalysisRunRequest,
  ProductAnalysisRunResponse,
  ProductAnalysisStatusResponse,
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
   * GET /run-team/jobs - list jobs.
   * @param runningOnly when true (default), only pending/running; when false, all jobs.
   */
  getRunningJobs(runningOnly = true): Observable<RunningJobsResponse> {
    const url = `${this.baseUrl}/run-team/jobs`;
    const params = runningOnly
      ? undefined
      : new HttpParams().set('running_only', 'false');
    return this.http.get<RunningJobsResponse>(url, params ? { params } : {});
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
   * POST /run-team/{job_id}/resume
   * Resume an interrupted run_team job (e.g. after server restart).
   */
  resumeRunTeamJob(jobId: string): Observable<RunTeamResponse> {
    return this.http.post<RunTeamResponse>(
      `${this.baseUrl}/run-team/${jobId}/resume`,
      {}
    );
  }

  /**
   * POST /run-team/{job_id}/restart
   * Start a brand-new job using the same repo as an existing terminal run_team job.
   */
  restartRunTeamJob(jobId: string): Observable<RunTeamResponse> {
    return this.http.post<RunTeamResponse>(
      `${this.baseUrl}/run-team/${jobId}/restart`,
      {}
    );
  }

  /**
   * POST /run-team/{job_id}/cancel
   * Request cancellation for a running or pending job.
   */
  cancelJob(jobId: string): Observable<{ job_id: string; status: string; message: string }> {
    return this.http.post<{ job_id: string; status: string; message: string }>(
      `${this.baseUrl}/run-team/${jobId}/cancel`,
      {}
    );
  }

  /**
   * POST /run-team/{job_id}/answers
   * Submit answers to pending questions to resume job execution.
   */
  submitAnswers(
    jobId: string,
    request: SubmitAnswersRequest
  ): Observable<JobStatusResponse> {
    return this.http.post<JobStatusResponse>(
      `${this.baseUrl}/run-team/${jobId}/answers`,
      request
    );
  }

  /**
   * POST /run-team/{job_id}/auto-answer/{question_id}
   * Auto-answer a pending question using LLM analysis.
   */
  autoAnswerRunTeam(
    jobId: string,
    questionId: string,
    request?: AutoAnswerRequest
  ): Observable<AutoAnswerResponse> {
    return this.http.post<AutoAnswerResponse>(
      `${this.baseUrl}/run-team/${jobId}/auto-answer/${questionId}`,
      request ?? {}
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
  // Frontend-Code-V2
  // -----------------------------------------------------------------------

  /**
   * POST /frontend-code-v2/run
   */
  runFrontendCodeV2(
    request: FrontendCodeV2RunRequest
  ): Observable<FrontendCodeV2RunResponse> {
    return this.http.post<FrontendCodeV2RunResponse>(
      `${this.baseUrl}/frontend-code-v2/run`,
      request
    );
  }

  /**
   * GET /frontend-code-v2/status/{job_id}
   */
  getFrontendCodeV2Status(
    jobId: string
  ): Observable<FrontendCodeV2StatusResponse> {
    return this.http.get<FrontendCodeV2StatusResponse>(
      `${this.baseUrl}/frontend-code-v2/status/${jobId}`
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
   * GET /planning-v2/jobs - list running and pending planning-v2 jobs.
   */
  getPlanningV2Jobs(): Observable<RunningJobsResponse> {
    return this.http.get<RunningJobsResponse>(
      `${this.baseUrl}/planning-v2/jobs`
    );
  }

  /**
   * GET /planning-v2/result/{job_id}
   */
  getPlanningV2Result(jobId: string): Observable<PlanningV2ResultResponse> {
    return this.http.get<PlanningV2ResultResponse>(
      `${this.baseUrl}/planning-v2/result/${jobId}`
    );
  }

  /**
   * POST /planning-v2/{job_id}/answers
   * Submit answers to open questions to resume planning-v2 workflow.
   */
  submitPlanningV2Answers(
    jobId: string,
    request: SubmitAnswersRequest
  ): Observable<PlanningV2StatusResponse> {
    return this.http.post<PlanningV2StatusResponse>(
      `${this.baseUrl}/planning-v2/${jobId}/answers`,
      request
    );
  }

  /**
   * POST /planning-v2/{job_id}/auto-answer/{question_id}
   * Auto-answer a pending question using LLM analysis.
   */
  autoAnswerPlanningV2(
    jobId: string,
    questionId: string,
    request?: AutoAnswerRequest
  ): Observable<AutoAnswerResponse> {
    return this.http.post<AutoAnswerResponse>(
      `${this.baseUrl}/planning-v2/${jobId}/auto-answer/${questionId}`,
      request ?? {}
    );
  }

  // -----------------------------------------------------------------------
  // Product Analysis
  // -----------------------------------------------------------------------

  /**
   * POST /product-analysis/run
   */
  runProductAnalysis(
    request: ProductAnalysisRunRequest
  ): Observable<ProductAnalysisRunResponse> {
    return this.http.post<ProductAnalysisRunResponse>(
      `${this.baseUrl}/product-analysis/run`,
      request
    );
  }

  /**
   * GET /product-analysis/status/{job_id}
   */
  getProductAnalysisStatus(
    jobId: string
  ): Observable<ProductAnalysisStatusResponse> {
    return this.http.get<ProductAnalysisStatusResponse>(
      `${this.baseUrl}/product-analysis/status/${jobId}`
    );
  }

  /**
   * GET /product-analysis/jobs - list running and pending product analysis jobs.
   */
  getProductAnalysisJobs(): Observable<RunningJobsResponse> {
    return this.http.get<RunningJobsResponse>(
      `${this.baseUrl}/product-analysis/jobs`
    );
  }

  /**
   * POST /product-analysis/{job_id}/answers
   * Submit answers to open questions to resume product analysis workflow.
   */
  submitProductAnalysisAnswers(
    jobId: string,
    request: SubmitAnswersRequest
  ): Observable<ProductAnalysisStatusResponse> {
    return this.http.post<ProductAnalysisStatusResponse>(
      `${this.baseUrl}/product-analysis/${jobId}/answers`,
      request
    );
  }

  /**
   * POST /product-analysis/{job_id}/auto-answer/{question_id}
   * Auto-answer a pending question using LLM analysis.
   */
  autoAnswerProductAnalysis(
    jobId: string,
    questionId: string,
    request?: AutoAnswerRequest
  ): Observable<AutoAnswerResponse> {
    return this.http.post<AutoAnswerResponse>(
      `${this.baseUrl}/product-analysis/${jobId}/auto-answer/${questionId}`,
      request ?? {}
    );
  }

  /**
   * GET /health
   */
  health(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.baseUrl}/health`);
  }
}
