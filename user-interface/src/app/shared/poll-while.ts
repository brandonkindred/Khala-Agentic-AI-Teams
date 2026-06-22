import { Observable, timer } from 'rxjs';
import { switchMap, takeWhile } from 'rxjs/operators';

/** Options for {@link pollWhile}. */
export interface PollWhileOptions {
  /** Delay between polls, in ms. Default 2000. */
  intervalMs?: number;
  /** Poll immediately on subscribe (default), or wait one interval first. */
  immediate?: boolean;
}

/**
 * Poll an async source on an interval until it reports a terminal value, then
 * complete. Replaces the hand-rolled `timer(0, N).pipe(switchMap(...))` + manual
 * stop logic copied across ~27 components.
 *
 * - Emits every poll result (including the terminal one), then completes.
 * - `switchMap` cancels an in-flight request when the next tick fires, so slow
 *   responses never overlap or arrive out of order.
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
  const { intervalMs = 2000, immediate = true } = options;
  return timer(immediate ? 0 : intervalMs, intervalMs).pipe(
    switchMap(() => fetch()),
    // `true` = inclusive: emit the terminal value, then complete.
    takeWhile((value) => !isDone(value), true),
  );
}
