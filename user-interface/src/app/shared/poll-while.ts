import { EMPTY, Observable, defer, throwError, timer } from 'rxjs';
import { catchError, concatMap, repeat, takeWhile, tap } from 'rxjs/operators';

/** Options for {@link pollWhile}. */
export interface PollWhileOptions {
  /** Delay between polls, in ms, measured from each poll's completion. Default 2000. */
  intervalMs?: number;
  /** Poll immediately on subscribe (default), or wait one interval first. */
  immediate?: boolean;
  /**
   * What to do when a poll's `fetch()` errors:
   * - `'continue'` (default): swallow the error, emit nothing for that poll, and
   *   keep polling — so a transient network blip doesn't kill live updates. This
   *   matches the `catchError(() => of(...))` pattern the hand-rolled pollers use.
   * - `'stop'`: let the error propagate and terminate the stream.
   */
  onError?: 'continue' | 'stop';
  /**
   * With `onError: 'continue'`, give up after this many *consecutive* failed
   * polls: the last error propagates and the stream terminates. A successful
   * poll resets the counter. Default `Infinity` (never give up) — pass a finite
   * cap when the poll can fail permanently (e.g. the polled resource was
   * deleted) and retrying forever would just spin a progress UI.
   */
  maxConsecutiveErrors?: number;
}

/**
 * Poll an async source until it reports a terminal value, then complete.
 * Replaces the hand-rolled `timer(0, N).pipe(switchMap(...))` + manual stop
 * logic copied across ~27 components.
 *
 * - Emits every poll result (including the terminal one), then completes.
 * - Polls run strictly one at a time: the next poll starts `intervalMs` after
 *   the previous one finishes (success or failure), so a response slower than
 *   the interval is simply awaited — never cancelled mid-flight, and responses
 *   can never overlap or arrive out of order.
 * - By default a failed poll is swallowed and polling continues (see
 *   `onError` / `maxConsecutiveErrors`); pass `onError: 'stop'` to terminate
 *   on the first error.
 * - Does NOT manage teardown — the caller adds `takeUntilDestroyed(destroyRef)`
 *   (or `takeUntil`) at the subscription so the poll stops with the component.
 *
 * Preconditions: `fetch` returns a cold Observable that completes;
 *   `intervalMs` (if given) is > 0; `maxConsecutiveErrors` (if given) is >= 1.
 * Postconditions: the returned Observable emits poll results in order and
 *   completes after the first value for which `isDone` returns true.
 *
 * @example
 * pollWhile(() => api.getStatus(id), s => isTerminal(s.status), { intervalMs: 2000 })
 *   .pipe(takeUntilDestroyed(this.destroyRef))
 *   .subscribe(s => this.status.set(s));
 */
export function pollWhile<T>(
  fetch: () => Observable<T>,
  isDone: (value: T) => boolean,
  options: PollWhileOptions = {},
): Observable<T> {
  const {
    intervalMs = 2000,
    immediate = true,
    onError = 'continue',
    maxConsecutiveErrors = Infinity,
  } = options;
  // Precondition: a non-positive interval would busy-loop; a cap below 1 could
  // never be reached meaningfully. Both are caller bugs — fail loudly.
  if (intervalMs <= 0) {
    throw new Error(`pollWhile: intervalMs must be > 0 (got ${intervalMs})`);
  }
  if (maxConsecutiveErrors < 1) {
    throw new Error(`pollWhile: maxConsecutiveErrors must be >= 1 (got ${maxConsecutiveErrors})`);
  }
  // defer() gives each subscription its own error counter.
  return defer(() => {
    let consecutiveErrors = 0;
    const poll = defer(fetch).pipe(
      tap(() => (consecutiveErrors = 0)),
      catchError((err) => {
        if (onError === 'stop' || ++consecutiveErrors >= maxConsecutiveErrors) {
          return throwError(() => err);
        }
        // EMPTY completes this poll without emitting; repeat() below schedules
        // the next one after the interval.
        return EMPTY;
      }),
    );
    return timer(immediate ? 0 : intervalMs).pipe(
      concatMap(() => poll.pipe(repeat({ delay: intervalMs }))),
      // `true` = inclusive: emit the terminal value, then complete.
      takeWhile((value) => !isDone(value), true),
    );
  });
}
