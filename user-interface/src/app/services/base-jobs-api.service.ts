import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { PollWhileOptions, pollWhile } from '../shared/poll-while';

/**
 * Abstract base for the per-team job API services. ~26 services currently
 * hand-roll the same `get status / cancel / delete / poll` HTTP shape over
 * `HttpClient` with no shared base. A concrete service supplies `jobUrl()` and
 * gets the common lifecycle methods for free; it can still add or override
 * team-specific endpoints (list shapes differ per team, so listing is left to
 * subclasses).
 *
 * `jobUrl()` is abstract on purpose: teams do NOT share a single-job status
 * route. There is no universal `GET /api/jobs/{team}/{id}` — the unified surface
 * only exposes list/cancel/delete — and each team mounts its own prefix
 * (`/assistant/jobs/{id}`, `/jobs/{id}`, `/teams/{id}/jobs/{id}`, …). cancelJob
 * and deleteJob are derived from `jobUrl()` because every team follows the same
 * REST shape relative to the job resource (`POST {jobUrl}/cancel`, `DELETE
 * {jobUrl}`); override them if a team diverges.
 *
 * @example
 * @Injectable({ providedIn: 'root' })
 * export class AssistantApiService extends BaseJobsApiService<AssistantJob> {
 *   protected jobUrl(id: string) {
 *     return `${API_BASE}/api/personal-assistant/assistant/jobs/${encodeURIComponent(id)}`;
 *   }
 * }
 */
@Injectable()
export abstract class BaseJobsApiService<TJob = unknown> {
  protected readonly http = inject(HttpClient);

  /** Absolute URL for a single job's status resource (encode the id here). */
  protected abstract jobUrl(jobId: string): string;

  /** Fetch a single job's current status by id. */
  getJob(jobId: string): Observable<TJob> {
    return this.http.get<TJob>(this.jobUrl(jobId));
  }

  /** Request cancellation of the job (`POST {jobUrl}/cancel`). */
  cancelJob(jobId: string): Observable<unknown> {
    return this.http.post(`${this.jobUrl(jobId)}/cancel`, {});
  }

  /** Delete the job resource (`DELETE {jobUrl}`). */
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
