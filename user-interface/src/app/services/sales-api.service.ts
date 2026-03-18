import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  SalesPipelineRequest,
  SalesPipelineRunResponse,
  SalesPipelineStatusResponse,
  SalesPipelineJobListItem,
  SalesPipelineResult,
  RecordStageOutcomeRequest,
  RecordDealOutcomeRequest,
  RecordOutcomeResponse,
  StageOutcome,
  DealOutcome,
  OutcomeSummary,
  LearningInsights,
  InsightsRefreshResponse,
  SalesHealthResponse,
} from '../models';

/**
 * Service for the AI Sales Team API.
 * Base URL from environment.salesApiUrl  →  /api/sales
 */
@Injectable({ providedIn: 'root' })
export class SalesApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.salesApiUrl;

  // -------------------------------------------------------------------------
  // Health
  // -------------------------------------------------------------------------

  health(): Observable<SalesHealthResponse> {
    return this.http.get<SalesHealthResponse>(`${this.baseUrl}/health`);
  }

  // -------------------------------------------------------------------------
  // Pipeline (async job-based)
  // -------------------------------------------------------------------------

  runPipeline(request: SalesPipelineRequest): Observable<SalesPipelineRunResponse> {
    return this.http.post<SalesPipelineRunResponse>(
      `${this.baseUrl}/sales/pipeline/run`,
      request
    );
  }

  getPipelineStatus(jobId: string): Observable<SalesPipelineStatusResponse> {
    return this.http.get<SalesPipelineStatusResponse>(
      `${this.baseUrl}/sales/pipeline/status/${jobId}`
    );
  }

  listPipelineJobs(runningOnly = false): Observable<SalesPipelineJobListItem[]> {
    const params = new HttpParams().set('running_only', String(runningOnly));
    return this.http.get<SalesPipelineJobListItem[]>(
      `${this.baseUrl}/sales/pipeline/jobs`,
      { params }
    );
  }

  cancelJob(jobId: string): Observable<unknown> {
    return this.http.post(
      `${this.baseUrl}/sales/pipeline/job/${jobId}/cancel`,
      {}
    );
  }

  deleteJob(jobId: string): Observable<unknown> {
    return this.http.delete(
      `${this.baseUrl}/sales/pipeline/job/${jobId}`
    );
  }

  // -------------------------------------------------------------------------
  // Outcomes
  // -------------------------------------------------------------------------

  recordStageOutcome(request: RecordStageOutcomeRequest): Observable<RecordOutcomeResponse> {
    return this.http.post<RecordOutcomeResponse>(
      `${this.baseUrl}/sales/outcomes/stage`,
      request
    );
  }

  recordDealOutcome(request: RecordDealOutcomeRequest): Observable<RecordOutcomeResponse> {
    return this.http.post<RecordOutcomeResponse>(
      `${this.baseUrl}/sales/outcomes/deal`,
      request
    );
  }

  getOutcomeSummary(): Observable<OutcomeSummary> {
    return this.http.get<OutcomeSummary>(`${this.baseUrl}/sales/outcomes/summary`);
  }

  listStageOutcomes(limit = 100): Observable<StageOutcome[]> {
    const params = new HttpParams().set('limit', String(limit));
    return this.http.get<StageOutcome[]>(`${this.baseUrl}/sales/outcomes/stage`, { params });
  }

  listDealOutcomes(limit = 100): Observable<DealOutcome[]> {
    const params = new HttpParams().set('limit', String(limit));
    return this.http.get<DealOutcome[]>(`${this.baseUrl}/sales/outcomes/deal`, { params });
  }

  // -------------------------------------------------------------------------
  // Learning Insights
  // -------------------------------------------------------------------------

  getInsights(): Observable<LearningInsights> {
    return this.http.get<LearningInsights>(`${this.baseUrl}/sales/insights`);
  }

  refreshInsights(): Observable<InsightsRefreshResponse> {
    return this.http.post<InsightsRefreshResponse>(
      `${this.baseUrl}/sales/insights/refresh`,
      {}
    );
  }
}
