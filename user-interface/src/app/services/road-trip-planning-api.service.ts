import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, Subject, timer } from 'rxjs';
import { switchMap, takeWhile, tap } from 'rxjs/operators';
import { environment } from '../../environments/environment';
import type {
  PlanJob,
  PlanJobSubmission,
  PlanTripRequestBody,
} from '../models';

/**
 * API client for the Road Trip Planning team.
 *
 * Backend exposes a one-shot planner (no conversational endpoint) —
 * this service wraps the async flow into a single observable stream
 * that emits every job poll until the job reaches a terminal state.
 */
@Injectable({ providedIn: 'root' })
export class RoadTripPlanningApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.roadTripPlanningApiUrl;

  /** Health check for the team's API. */
  getHealth(): Observable<{ status?: string }> {
    return this.http.get<{ status?: string }>(`${this.baseUrl}/health`);
  }

  /** Submit a planning job. Returns immediately with a job_id. */
  submitPlanJob(body: PlanTripRequestBody): Observable<PlanJobSubmission> {
    return this.http.post<PlanJobSubmission>(`${this.baseUrl}/plan`, body);
  }

  /** Single poll of a job's status. */
  getJob(jobId: string): Observable<PlanJob> {
    return this.http.get<PlanJob>(`${this.baseUrl}/jobs/${jobId}`);
  }

  /**
   * Submit a plan job and emit every poll result until it completes or
   * fails. The final emission is the terminal `PlanJob` (status `completed`
   * or `failed`). Terminates the subscription on terminal status.
   *
   * @param body       Plan request
   * @param pollMs     Poll interval (default 2000ms)
   * @param onSubmit   Optional hook fired once with the submission id.
   */
  planAndPoll(
    body: PlanTripRequestBody,
    pollMs = 2000,
    onSubmit?: (submission: PlanJobSubmission) => void,
  ): Observable<PlanJob> {
    const submission$ = new Subject<PlanJobSubmission>();
    const stream$ = this.submitPlanJob(body).pipe(
      tap((submission) => {
        onSubmit?.(submission);
        submission$.next(submission);
        submission$.complete();
      }),
      switchMap((submission) =>
        timer(0, pollMs).pipe(
          switchMap(() => this.getJob(submission.job_id)),
          takeWhile((job) => job.status !== 'completed' && job.status !== 'failed', true),
        ),
      ),
    );
    return stream$;
  }
}
