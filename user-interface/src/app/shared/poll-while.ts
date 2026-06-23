import { EMPTY, Observable, timer } from 'rxjs';
import { catchError, switchMap, takeWhile } from 'rxjs/operators';

/** Options for {@link pollWhile}. */
export interface PollWhileOptions {
  /** Delay between polls, in ms. Default 2000. */
  intervalMs?: number;
  /** Poll immediately on subscribe (default), or wait one interval first. */
  immediate?: boolean;
  /**
   * What to do when a poll's `fetch()` errors:
   * - `'continue'` (default): swallow the error, emit nothing for that tick, and
   *   keep polling — so a transient network blip doesn't kill live updates. This
   *   matches the `catchError(() => of(...))` pattern the hand-rolled pollers use.
   * - `'stop'`: let the error propagate and terminate the stream.
   */
  onError?: 'continue' | 'stop';
}

/**
 * Poll an async source on an interval until it reports a terminal value, then
 * complete. Replaces the hand-rolled `timer(0, N).pipe(switchMap(...))` + manual
 * stop logic copied across ~27 components.
 *
 * - Emits every poll result (including the terminal one), then completes.
 * - `switchMap` cancels an in-flight request when the next tick fires, so slow
 *   responses never overlap or arrive out of order.
 * - By default a failed poll is swallowed and polling continues (see
 *   `onError`); pass `onError: 'stop'` to terminate on the first error.
 * - Does NOT manage teardown — the caller adds `takeUntilDestroyed(destroyRef)`
 *   (or `takeUntil`) at the subscription so the poll stops with the component.
 *
 * Preconditions: `fetch` returns a cold Observable that completes;
 *   `intervalMs` (if given) is > 0.
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
  const { intervalMs = 2000, immediate = true, onError = 'continue' } = options;
  return timer(immediate ? 0 : intervalMs, intervalMs).pipe(
    switchMap(() =>
      // On error, EMPTY completes only the inner poll (no emission); the outer
      // timer keeps ticking, so the next interval retries.
      onError === 'stop' ? fetch() : fetch().pipe(catchError(() => EMPTY)),
    ),
    // `true` = inclusive: emit the terminal value, then complete.
    takeWhile((value) => !isDone(value), true),
  );
}
