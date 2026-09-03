/** Quiet period a differing value must hold before a SettleAnnouncer fires its `onSettle`
 *  callback — long enough to outlast a single poll interval, so a screen reader user gets one
 *  followable announcement per lull instead of one per poll tick. Shared by
 *  coding-team-page.component.ts's thinking announcer and coding-team-monitor.component.ts's
 *  summary announcer so the two quiet windows cannot silently drift apart. */
export const ANNOUNCE_SETTLE_MS = 1500;

/**
 * Debounces a rapidly-changing text value (e.g. fed by a poll loop) into occasional "settled"
 * announcements for a polite `aria-live` region, replacing the hand-rolled
 * `setTimeout`/`clearTimeout` bookkeeping previously duplicated across
 * `coding-team-page.component.ts` (`updateThinkingAnnouncement`) and
 * `coding-team-monitor.component.ts` (`scheduleAnnouncement`).
 *
 * @example
 * private readonly announcer = new SettleAnnouncer(ANNOUNCE_SETTLE_MS, (value) => this.text = value);
 * // on each new poll value:
 * this.announcer.update(nextValue);
 * // on destroy:
 * this.announcer.dispose();
 */
export class SettleAnnouncer {
  private previousValue = '';
  private timer: ReturnType<typeof setTimeout> | null = null;

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
   * Postconditions: when `nextValue` is '', any pending settle timer is cleared, the last-seen value
   * is reset to '' (so the same value reappearing later, e.g. a restarted run with the same
   * objective, is treated as new rather than remembered across the clear), and `onSettle('')` fires
   * immediately. When `nextValue` equals the last-seen value, any pending settle timer is left
   * completely untouched — neither restarted nor cancelled — and neither callback fires. Otherwise
   * (a differing, non-empty value) any pending settle timer is replaced with a fresh one, `onChange`
   * fires synchronously if given, and after `settleMs` with no further differing update, `onSettle`
   * fires with `nextValue` and the timer handle is cleared.
   */
  update(nextValue: string): void {
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
   *  null, so it can never fire after the owning view/component is destroyed. */
  dispose(): void {
    this.clearTimer();
  }

  private clearTimer(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}
