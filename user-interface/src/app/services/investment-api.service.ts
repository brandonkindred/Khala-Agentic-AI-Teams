import { Injectable, NgZone, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  CreateProfileRequest,
  CreateProfileResponse,
  GetProfileResponse,
  CreateProposalRequest,
  CreateProposalResponse,
  GetProposalResponse,
  ValidateProposalRequest,
  ValidateProposalResponse,
  CreateStrategyRequest,
  CreateStrategyResponse,
  ValidateStrategyRequest,
  ValidateStrategyResponse,
  PromotionDecisionRequest,
  PromotionDecisionResponse,
  WorkflowStatusResponse,
  QueuesResponse,
  CreateMemoRequest,
  CreateMemoResponse,
  InvestmentHealthResponse,
  RunStrategyLabRequest,
  StrategyLabRunStartResponse,
  StrategyLabResultsResponse,
  StrategyLabRunStatus,
  ActiveRunsResponse,
  StrategyLabStreamEvent,
  InvestmentJobsListResponse,
  DeleteStrategyLabRecordResponse,
  ClearStrategyLabStorageResponse,
  StartAdvisorSessionRequest,
  SendAdvisorMessageRequest,
  AdvisorSessionResponse,
  AdvisorSessionStateResponse,
  CompleteAdvisorSessionResponse,
} from '../models';

/**
 * Service for Investment Team API endpoints.
 * Base URL from environment.investmentApiUrl.
 */
@Injectable({ providedIn: 'root' })
export class InvestmentApiService {
  private readonly http = inject(HttpClient);
  private readonly zone = inject(NgZone);
  private readonly baseUrl = environment.investmentApiUrl;

  // ---------------------------------------------------------------------------
  // Health Check
  // ---------------------------------------------------------------------------

  healthCheck(): Observable<InvestmentHealthResponse> {
    return this.http.get<InvestmentHealthResponse>(`${this.baseUrl}/health`);
  }

  // ---------------------------------------------------------------------------
  // Profiles / IPS
  // ---------------------------------------------------------------------------

  createProfile(request: CreateProfileRequest): Observable<CreateProfileResponse> {
    return this.http.post<CreateProfileResponse>(
      `${this.baseUrl}/profiles`,
      request
    );
  }

  getProfile(userId: string): Observable<GetProfileResponse> {
    return this.http.get<GetProfileResponse>(
      `${this.baseUrl}/profiles/${userId}`
    );
  }

  // ---------------------------------------------------------------------------
  // Portfolio Proposals
  // ---------------------------------------------------------------------------

  createProposal(request: CreateProposalRequest): Observable<CreateProposalResponse> {
    return this.http.post<CreateProposalResponse>(
      `${this.baseUrl}/proposals/create`,
      request
    );
  }

  getProposal(proposalId: string): Observable<GetProposalResponse> {
    return this.http.get<GetProposalResponse>(
      `${this.baseUrl}/proposals/${proposalId}`
    );
  }

  validateProposal(
    proposalId: string,
    request: ValidateProposalRequest
  ): Observable<ValidateProposalResponse> {
    return this.http.post<ValidateProposalResponse>(
      `${this.baseUrl}/proposals/${proposalId}/validate`,
      request
    );
  }

  // ---------------------------------------------------------------------------
  // Strategies
  // ---------------------------------------------------------------------------

  createStrategy(request: CreateStrategyRequest): Observable<CreateStrategyResponse> {
    return this.http.post<CreateStrategyResponse>(
      `${this.baseUrl}/strategies`,
      request
    );
  }

  validateStrategy(
    strategyId: string,
    request?: ValidateStrategyRequest
  ): Observable<ValidateStrategyResponse> {
    return this.http.post<ValidateStrategyResponse>(
      `${this.baseUrl}/strategies/${strategyId}/validate`,
      request ?? {}
    );
  }

  // ---------------------------------------------------------------------------
  // Promotion
  // ---------------------------------------------------------------------------

  promotionDecision(request: PromotionDecisionRequest): Observable<PromotionDecisionResponse> {
    return this.http.post<PromotionDecisionResponse>(
      `${this.baseUrl}/promotions/decide`,
      request
    );
  }

  // ---------------------------------------------------------------------------
  // Workflow
  // ---------------------------------------------------------------------------

  getWorkflowStatus(): Observable<WorkflowStatusResponse> {
    return this.http.get<WorkflowStatusResponse>(
      `${this.baseUrl}/workflow/status`
    );
  }

  getQueues(): Observable<QueuesResponse> {
    return this.http.get<QueuesResponse>(
      `${this.baseUrl}/workflow/queues`
    );
  }

  // ---------------------------------------------------------------------------
  // Memos
  // ---------------------------------------------------------------------------

  createMemo(request: CreateMemoRequest): Observable<CreateMemoResponse> {
    return this.http.post<CreateMemoResponse>(
      `${this.baseUrl}/memos`,
      request
    );
  }

  // ---------------------------------------------------------------------------
  // Strategy Lab
  // ---------------------------------------------------------------------------

  runStrategyLab(request?: RunStrategyLabRequest): Observable<StrategyLabRunStartResponse> {
    return this.http.post<StrategyLabRunStartResponse>(
      `${this.baseUrl}/strategy-lab/run`,
      request ?? {}
    );
  }

  getActiveRuns(): Observable<ActiveRunsResponse> {
    return this.http.get<ActiveRunsResponse>(`${this.baseUrl}/strategy-lab/runs`);
  }

  getRunStatus(runId: string): Observable<StrategyLabRunStatus> {
    return this.http.get<StrategyLabRunStatus>(
      `${this.baseUrl}/strategy-lab/runs/${encodeURIComponent(runId)}/status`
    );
  }

  streamRunStatus(runId: string): Observable<StrategyLabStreamEvent> {
    return new Observable<StrategyLabStreamEvent>((subscriber) => {
      const url = `${this.baseUrl}/strategy-lab/runs/${encodeURIComponent(runId)}/stream`;
      const eventSource = new EventSource(url);
      eventSource.onmessage = (msg) => {
        this.zone.run(() => {
          try {
            const data: StrategyLabStreamEvent = JSON.parse(msg.data);
            subscriber.next(data);
            if (data.type === 'done') {
              eventSource.close();
              subscriber.complete();
            }
          } catch {
            // Ignore unparseable frames (keepalive comments)
          }
        });
      };
      eventSource.onerror = () => {
        eventSource.close();
        this.zone.run(() => subscriber.error(new Error('SSE connection lost')));
      };
      return () => eventSource.close();
    });
  }

  resumeRun(runId: string): Observable<StrategyLabRunStartResponse> {
    return this.http.post<StrategyLabRunStartResponse>(
      `${this.baseUrl}/strategy-lab/runs/${encodeURIComponent(runId)}/resume`,
      {}
    );
  }

  restartRun(runId: string): Observable<StrategyLabRunStartResponse> {
    return this.http.post<StrategyLabRunStartResponse>(
      `${this.baseUrl}/strategy-lab/runs/${encodeURIComponent(runId)}/restart`,
      {}
    );
  }

  listStrategyLabJobs(runningOnly = false): Observable<InvestmentJobsListResponse> {
    return this.http.get<InvestmentJobsListResponse>(
      `${this.baseUrl}/strategy-lab/jobs`,
      { params: runningOnly ? { running_only: 'true' } : {} }
    );
  }

  getStrategyLabResults(winning?: boolean): Observable<StrategyLabResultsResponse> {
    const params: Record<string, string> = {};
    if (winning !== undefined) {
      params['winning'] = String(winning);
    }
    return this.http.get<StrategyLabResultsResponse>(
      `${this.baseUrl}/strategy-lab/results`,
      { params }
    );
  }

  deleteStrategyLabRecord(labRecordId: string): Observable<DeleteStrategyLabRecordResponse> {
    return this.http.delete<DeleteStrategyLabRecordResponse>(
      `${this.baseUrl}/strategy-lab/records/${encodeURIComponent(labRecordId)}`
    );
  }

  clearStrategyLabStorage(): Observable<ClearStrategyLabStorageResponse> {
    return this.http.delete<ClearStrategyLabStorageResponse>(
      `${this.baseUrl}/strategy-lab/storage`
    );
  }

  // ---------------------------------------------------------------------------
  // Financial Advisor (Chat)
  // ---------------------------------------------------------------------------

  startAdvisorSession(request: StartAdvisorSessionRequest): Observable<AdvisorSessionResponse> {
    return this.http.post<AdvisorSessionResponse>(
      `${this.baseUrl}/advisor/sessions`,
      request
    );
  }

  sendAdvisorMessage(
    sessionId: string,
    request: SendAdvisorMessageRequest
  ): Observable<AdvisorSessionResponse> {
    return this.http.post<AdvisorSessionResponse>(
      `${this.baseUrl}/advisor/sessions/${sessionId}/messages`,
      request
    );
  }

  getAdvisorSession(sessionId: string): Observable<AdvisorSessionStateResponse> {
    return this.http.get<AdvisorSessionStateResponse>(
      `${this.baseUrl}/advisor/sessions/${sessionId}`
    );
  }

  completeAdvisorSession(sessionId: string): Observable<CompleteAdvisorSessionResponse> {
    return this.http.post<CompleteAdvisorSessionResponse>(
      `${this.baseUrl}/advisor/sessions/${sessionId}/complete`,
      {}
    );
  }
}
