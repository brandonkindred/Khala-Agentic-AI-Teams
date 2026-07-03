import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable, of, throwError } from 'rxjs';
import { first, switchMap } from 'rxjs/operators';
import { environment } from '../../environments/environment';
import { pollWhile } from '../shared/poll-while';
import type {
  HealthResponse,
  JobMatchRequest,
  JobMatchResponse,
  JobMatchRunDetail,
  JobMatchRunSummary,
  JobMatchScanJob,
  JobSeekerProfile,
  Listing,
  ListingFilter,
  ListingStateUpdate,
  ListingsResponse,
  ScanJobListItem,
} from '../models';

const POLL_INTERVAL_MS = 2000;
/** Consecutive failed status polls tolerated (~10s) before the scan errors out. */
const MAX_POLL_ERRORS = 5;
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled']);

/**
 * Service for the Job Matching API (scan for open roles, manage listings,
 * edit the career profile). Base URL from environment.jobMatchingApiUrl.
 */
@Injectable({ providedIn: 'root' })
export class JobMatchingApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.jobMatchingApiUrl;

  /** GET /health */
  health(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.baseUrl}/health`);
  }

  /** GET /profile — the resolved career profile. */
  getProfile(): Observable<JobSeekerProfile> {
    return this.http.get<JobSeekerProfile>(`${this.baseUrl}/profile`);
  }

  /** PUT /profile — save as the career section of the user profile. */
  saveProfile(profile: JobSeekerProfile): Observable<JobSeekerProfile> {
    return this.http.put<JobSeekerProfile>(`${this.baseUrl}/profile`, profile);
  }

  /** POST /scan — start an async scan; returns `{job_id, status}` immediately. */
  startScan(request: JobMatchRequest): Observable<{ job_id: string; status: string }> {
    return this.http.post<{ job_id: string; status: string }>(`${this.baseUrl}/scan`, request);
  }

  /** GET /scan/status/{jobId} — single status poll. */
  getScanStatus(jobId: string): Observable<JobMatchScanJob> {
    return this.http.get<JobMatchScanJob>(`${this.baseUrl}/scan/status/${jobId}`);
  }

  /**
   * Poll a scan to its terminal state and emit the final result. Built on the
   * shared pollWhile operator, so a transient failed poll is swallowed and
   * polling continues — one network blip must not report a running scan as
   * failed. A permanent failure (job deleted, API down) still surfaces: after
   * MAX_POLL_ERRORS consecutive failed polls the error propagates instead of
   * spinning the progress UI forever. Errors also when the scan itself ends
   * `failed`/`cancelled`.
   */
  pollScan(jobId: string): Observable<JobMatchResponse> {
    return pollWhile(
      () => this.getScanStatus(jobId),
      (job) => TERMINAL_STATUSES.has(job.status),
      { intervalMs: POLL_INTERVAL_MS, maxConsecutiveErrors: MAX_POLL_ERRORS }
    ).pipe(
      first((job) => TERMINAL_STATUSES.has(job.status)),
      switchMap((job) =>
        job.status === 'completed' && job.result
          ? of(job.result)
          : throwError(() => new Error(job.error || `Job matching scan ${job.status}`))
      )
    );
  }

  /** GET /scan/jobs */
  listScanJobs(runningOnly = false): Observable<{ jobs: ScanJobListItem[] }> {
    const params = new HttpParams().set('running_only', String(runningOnly));
    return this.http.get<{ jobs: ScanJobListItem[] }>(`${this.baseUrl}/scan/jobs`, { params });
  }

  /** POST /scan/jobs/{jobId}/cancel */
  cancelScanJob(jobId: string): Observable<{ job_id: string; success: boolean }> {
    return this.http.post<{ job_id: string; success: boolean }>(
      `${this.baseUrl}/scan/jobs/${jobId}/cancel`,
      {}
    );
  }

  /** DELETE /scan/jobs/{jobId} */
  deleteScanJob(jobId: string): Observable<{ job_id: string; deleted: boolean }> {
    return this.http.delete<{ job_id: string; deleted: boolean }>(
      `${this.baseUrl}/scan/jobs/${jobId}`
    );
  }

  /** GET /runs */
  listRuns(limit = 50): Observable<JobMatchRunSummary[]> {
    const params = new HttpParams().set('limit', String(limit));
    return this.http.get<JobMatchRunSummary[]>(`${this.baseUrl}/runs`, { params });
  }

  /** GET /runs/{runId} */
  getRun(runId: string): Observable<JobMatchRunDetail> {
    return this.http.get<JobMatchRunDetail>(`${this.baseUrl}/runs/${runId}`);
  }

  /** GET /listings — aggregated listings (latest per fingerprint) plus counts. */
  listListings(status: ListingFilter = 'active', limit = 200): Observable<ListingsResponse> {
    const params = new HttpParams().set('status', status).set('limit', String(limit));
    return this.http.get<ListingsResponse>(`${this.baseUrl}/listings`, { params });
  }

  /** PATCH /listings/{fingerprint} — set a listing's user status/notes. */
  updateListing(fingerprint: string, update: ListingStateUpdate): Observable<Listing> {
    return this.http.patch<Listing>(`${this.baseUrl}/listings/${fingerprint}`, update);
  }
}
