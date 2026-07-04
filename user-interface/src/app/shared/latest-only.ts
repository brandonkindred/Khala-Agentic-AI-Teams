/**
 * Monotonic "latest wins" guard for out-of-order async responses.
 *
 * Overlapping requests (rapid filter switches, several refreshes in flight) can
 * resolve out of order, letting a stale response overwrite a newer one. Claim a
 * token with {@link next} before each request and gate the handler on
 * {@link isCurrent}; only the most recently issued token is current, so a slow
 * response that lands after a newer request is discarded.
 *
 * Replaces the hand-rolled `const seq = ++this.xSeq; … if (seq === this.xSeq)`
 * pattern duplicated across components.
 *
 * @example
 * private readonly load = new LatestOnly();
 * const token = this.load.next();
 * this.api.get().subscribe(res => { if (this.load.isCurrent(token)) this.apply(res); });
 */
export class LatestOnly {
  private seq = 0;

  /** Claim and return the newest token, superseding all prior ones. */
  next(): number {
    return ++this.seq;
  }

  /** True only for the most recently issued token. */
  isCurrent(token: number): boolean {
    return token === this.seq;
  }
}
