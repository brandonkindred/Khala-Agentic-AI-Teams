import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { PollWhileOptions, pollWhile } from '../shared/poll-while';

/**
 * Abstract base for the per-team job API services. ~26 services currently
 * hand-roll the same `get status / cancel / delete / poll` HTTP shape over
 * `HttpClient` with no shared base. A concrete service supplies its `baseUrl`
 * and gets the common lifecycle methods for free; it can still add or override
 * team-specific endpoints (list shapes differ per team, so listing is left to
 * subclasses).
 *
 * @example
 * @Injectable({ providedIn: 'root' })
 * export class SalesApiService extends BaseJobsApiService<SalesJob> {
 *   protected readonly baseUrl = `${API_BASE}/api/sales`;
 * }
 */
@Injectable()
export abstract class BaseJobsApiService<TJob = unknown> {
  protected readonly http = inject(HttpClient);

  /** Root URL for this team's job endpoints, e.g. `${API_BASE}/api/sales`. */
  protected abstract readonly baseUrl: string;

  /** URL for a single job. Override if the team uses a different convention. */
  protected jobUrl(jobId: string): string {
    return `${this.baseUrl}/jobs/${encodeURIComponent(jobId)}`;
  }

  getJob(jobId: string): Observable<TJob> {
    return this.http.get<TJob>(this.jobUrl(jobId));
  }

  cancelJob(jobId: string): Observable<unknown> {
    return this.http.post(`${this.jobUrl(jobId)}/cancel`, {});
  }

  deleteJob(jobId: string): Observable<unknown> {
    return this.http.delete(this.jobUrl(jobId));
  }

  /**
   * Poll a job until `isDone`, then complete — using the shared `pollWhile`
   * operator. The caller adds `takeUntilDestroyed(...)` at the subscription.
   */
  pollJob(
    jobId: string,
    isDone: (job: TJob) => boolean,
    options?: PollWhileOptions,
  ): Observable<TJob> {
    return pollWhile(() => this.getJob(jobId), isDone, options);
  }
}
