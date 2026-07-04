/**
 * Focus a resolved element within `host` after the current render tick.
 *
 * Components that remove/replace a control (triage a card, cancel a scan job)
 * must move keyboard focus to a sensible neighbour once Angular has re-rendered.
 * The one-tick `setTimeout` waits for that render; `resolve` picks the target
 * (with any fallback logic inline) and may return null when nothing suitable
 * remains. Returns the timer handle so a component destroyed within the tick can
 * clear it.
 *
 * @example
 * deferFocus(this.host.nativeElement, h =>
 *   h.querySelector<HTMLElement>('.next-row') ?? h.querySelector<HTMLElement>('#heading'));
 */
export function deferFocus(
  host: HTMLElement,
  resolve: (host: HTMLElement) => HTMLElement | null | undefined,
): ReturnType<typeof setTimeout> {
  return setTimeout(() => resolve(host)?.focus());
}
