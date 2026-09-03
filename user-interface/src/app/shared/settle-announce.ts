/**
 * Debounces a rapidly-changing text value (e.g. fed by a poll loop or a token stream) into
 * occasional "settled" announcements for a polite `aria-live` region, replacing the hand-rolled
 * `setTimeout`/`clearTimeout` bookkeeping previously duplicated across
 * `coding-team-page.component.ts` (`updateThinkingAnnouncement`) and
 * `coding-team-monitor.component.ts` (`scheduleAnnouncement`).
 *
 * This class owns only the timer bookkeeping (unchanged/differing/settle/dispose), not any
 * particular quiet-period value: for the debounce to actually coalesce updates rather than just
 * add a fixed delay to each one, `settleMs` must exceed the real gap between the source's updates
 * (e.g. a poll interval), which differs per consumer — a fast raw token stream and a 5-second HTTP
 * poll need very different windows. Each call site therefore supplies (and documents) its own
 * `settleMs`, derived from its own update cadence, rather than sharing one constant.
 *
 * @example
 * private readonly announcer = new SettleAnnouncer(MY_SETTLE_MS, (value) => this.text = value);
 * // on each new poll value:
 * this.announcer.update(nextValue);
 * // on destroy:
 * this.announcer.dispose();
 */
export class SettleAnnouncer {
  private previousValue = '';
  private timer: ReturnType<typeof setTimeout> | null = null;
  private disposed = false;

  /**
   * Preconditions: `settleMs` is a positive number of milliseconds. `onSettle` is called with the
   * settled value once it has held for `settleMs` with no further differing update, and with '' when
   * `update('')` is called (immediately, not after a delay — see `update`). `onChange`, if given, is
   * called synchronously inside `update` whenever a differing, non-empty value is first accepted,
   * before the settle timer is (re)armed — for a caller that wants an immediate provisional cue (e.g.
   * "Agent is thinking…") ahead of the eventual settled announcement; omit it for a caller that wants
   * no announcement at all until the value has settled.
   */
  constructor(
    private readonly settleMs: number,
    private readonly onSettle: (value: string) => void,
    private readonly onChange?: (value: string) => void,
  ) {}

  /** True while a settle timer is pending (a differing, non-empty update is awaiting its quiet
   *  window). Exposed so callers/tests can assert on debounce state without reaching into private
   *  fields. */
  get isPending(): boolean {
    return this.timer !== null;
  }

  /**
   * Preconditions: none — `nextValue` may be '' or any string.
   * Postconditions: once `dispose()` has been called, this is permanently a no-op — neither
   * callback fires and no timer is armed, so a call arriving after teardown can never resurrect a
   * timer against a destroyed view. Otherwise: when `nextValue` is '', any pending settle timer is
   * cleared, the last-seen value is reset to '' (so the same value reappearing later, e.g. a
   * restarted run with the same objective, is treated as new rather than remembered across the
   * clear), and `onSettle('')` fires immediately. When `nextValue` equals the last-seen value, any
   * pending settle timer is left completely untouched — neither restarted nor cancelled — and
   * neither callback fires. Otherwise (a differing, non-empty value) any pending settle timer is
   * replaced with a fresh one, `onChange` fires synchronously if given, and after `settleMs` with no
   * further differing update, `onSettle` fires with `nextValue` and the timer handle is cleared.
   */
  update(nextValue: string): void {
    if (this.disposed) {
      return;
    }
    if (!nextValue) {
      this.clearTimer();
      this.previousValue = '';
      this.onSettle('');
      return;
    }
    if (nextValue === this.previousValue) {
      return;
    }
    this.previousValue = nextValue;
    this.clearTimer();
    this.onChange?.(nextValue);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.onSettle(nextValue);
    }, this.settleMs);
  }

  /** Preconditions: none. Postconditions: any pending settle timer is cleared and its handle set to
   *  null. The instance is permanently disposed — every subsequent `update()` call is a no-op — so
   *  it can never fire, or be re-armed, after the owning view/component is destroyed. */
  dispose(): void {
    this.disposed = true;
    this.clearTimer();
  }

  private clearTimer(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}
