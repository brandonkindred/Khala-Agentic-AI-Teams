import { EMPTY, Observable, Subject, Subscription, interval } from 'rxjs';
import { catchError, switchMap, takeUntil, takeWhile, tap } from 'rxjs/operators';
import type { CodingTeamJobStatus } from '../models/coding-team.model';
import { isCodingTeamTerminalStatus } from '../models/job-status.model';

/** The slice of the coding-team API a poller needs. */
export interface JobStatusSource {
  getJobStatus(jobId: string): Observable<CodingTeamJobStatus>;
}

/**
 * Poll a coding-team job's status until it reaches a terminal state.
 *
 * Emits each fetched status to `onStatus`; after `maxConsecutiveErrors` consecutive
 * fetch failures it calls `onConnectionLost` and stops. Polling also stops once the
 * status is terminal. Returns the `Subscription` so the caller can tear it down on
 * destroy. Shared by the Coding Team and Code Review panels so the interval, error
 * budget, and terminal-status handling stay consistent.
 */
export function pollJobStatus(
  api: JobStatusSource,
  jobId: string,
  onStatus: (status: CodingTeamJobStatus) => void,
  onConnectionLost: () => void,
  intervalMs = 5000,
  maxConsecutiveErrors = 3,
): Subscription {
  let errors = 0;
  const stop$ = new Subject<void>();
  return interval(intervalMs)
    .pipe(
      switchMap(() =>
        api.getJobStatus(jobId).pipe(
          catchError(() => {
            errors++;
            if (errors >= maxConsecutiveErrors) {
              onConnectionLost();
              stop$.next();
            }
            return EMPTY;
          }),
        ),
      ),
      tap(() => {
        errors = 0;
      }),
      takeWhile((status) => !isCodingTeamTerminalStatus(status.status), true),
      takeUntil(stop$),
    )
    .subscribe({ next: onStatus });
}
