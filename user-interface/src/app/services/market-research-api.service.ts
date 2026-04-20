import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, EMPTY, of, throwError, timer } from 'rxjs';
import { expand, first, switchMap } from 'rxjs/operators';
import { environment } from '../../environments/environment';
import type {
  RunMarketResearchRequest,
  TeamOutput,
  HealthResponse,
} from '../models';

interface MarketResearchJobSubmission {
  job_id: string;
  status: string;
}

interface MarketResearchJobStatus {
  job_id: string;
  status: string;
  progress?: number | null;
  result?: TeamOutput | null;
  error?: string | null;
}

const POLL_INTERVAL_MS = 2000;

/**
 * Service for Market Research API endpoints.
 * Base URL from environment.marketResearchApiUrl (default port 8011).
 */
@Injectable({ providedIn: 'root' })
export class MarketResearchApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.marketResearchApiUrl;

  /**
   * Submit a market-research job. Returns `{job_id, status}` immediately.
   */
  submitRun(request: RunMarketResearchRequest): Observable<MarketResearchJobSubmission> {
    return this.http.post<MarketResearchJobSubmission>(
      `${this.baseUrl}/market-research/run`,
      request
    );
  }

  /** Single status poll. */
  getStatus(jobId: string): Observable<MarketResearchJobStatus> {
    return this.http.get<MarketResearchJobStatus>(
      `${this.baseUrl}/market-research/status/${jobId}`
    );
  }

  /**
   * Submit a market-research run and emit the final `TeamOutput` when the
   * job completes. Errors if the job ends in `failed` or `cancelled`.
   */
  run(request: RunMarketResearchRequest): Observable<TeamOutput> {
    return this.submitRun(request).pipe(
      switchMap((submission) => this.pollJob(submission.job_id))
    );
  }

  private pollJob(jobId: string): Observable<TeamOutput> {
    const poll$ = this.getStatus(jobId);
    return poll$.pipe(
      expand((job) =>
        job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'
          ? EMPTY
          : timer(POLL_INTERVAL_MS).pipe(switchMap(() => poll$))
      ),
      first((job) =>
        job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'
      ),
      switchMap((job) =>
        job.status === 'completed' && job.result
          ? of(job.result)
          : throwError(() => new Error(job.error || `Market research job ${job.status}`))
      )
    );
  }

  /** GET /health */
  health(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.baseUrl}/health`);
  }
}
