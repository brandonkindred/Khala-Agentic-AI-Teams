import { vi } from 'vitest';
import { SettleAnnouncer } from './settle-announce';

describe('SettleAnnouncer', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('does not announce before the settle window elapses', () => {
    vi.useFakeTimers();
    const onSettle = vi.fn();
    const announcer = new SettleAnnouncer(1500, onSettle);
    announcer.update('a');
    expect(onSettle).not.toHaveBeenCalled();
    expect(announcer.isPending).toBe(true);
  });

  it('announces the value once the settle window elapses with no further update', async () => {
    vi.useFakeTimers();
    const onSettle = vi.fn();
    const announcer = new SettleAnnouncer(1500, onSettle);
    announcer.update('a');
    await vi.advanceTimersByTimeAsync(1500);
    expect(onSettle).toHaveBeenCalledExactlyOnceWith('a');
    expect(announcer.isPending).toBe(false);
  });

  it('leaves an in-flight timer completely untouched when the value is unchanged', async () => {
    vi.useFakeTimers();
    const onSettle = vi.fn();
    const announcer = new SettleAnnouncer(1500, onSettle);
    announcer.update('a');
    await vi.advanceTimersByTimeAsync(1000);
    announcer.update('a'); // unchanged — must not restart the window
    await vi.advanceTimersByTimeAsync(500); // total 1500 since the original update
    expect(onSettle).toHaveBeenCalledExactlyOnceWith('a');
  });

  it('replaces (restarts) the pending timer on a differing update, proven against the ORIGINAL deadline', async () => {
    // A weaker "keep the original timer, just announce the latest value when it fires" implementation
    // would pass a test that only checks the value at the end — this pins the actual restart by
    // checking silence at the point the original (unreplaced) deadline would have fired.
    vi.useFakeTimers();
    const onSettle = vi.fn();
    const announcer = new SettleAnnouncer(1500, onSettle);

    announcer.update('a'); // t=0, original deadline would be t=1500
    await vi.advanceTimersByTimeAsync(1000); // t=1000
    announcer.update('b'); // differs — restarts the window, new deadline t=2500

    await vi.advanceTimersByTimeAsync(600); // t=1600 — past the ORIGINAL deadline
    expect(onSettle).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(900); // t=2500 — the restarted deadline
    expect(onSettle).toHaveBeenCalledExactlyOnceWith('b');
  });

  it('clears any pending timer and announces empty immediately when the value is cleared', async () => {
    vi.useFakeTimers();
    const onSettle = vi.fn();
    const announcer = new SettleAnnouncer(1500, onSettle);
    announcer.update('a');
    expect(announcer.isPending).toBe(true);

    announcer.update('');
    expect(onSettle).toHaveBeenCalledExactlyOnceWith('');
    expect(announcer.isPending).toBe(false);

    // No lingering timer from the cleared update.
    await vi.advanceTimersByTimeAsync(1500);
    expect(onSettle).toHaveBeenCalledTimes(1);
  });

  it('treats a value reappearing after a clear as new, not remembered across the clear', async () => {
    vi.useFakeTimers();
    const onSettle = vi.fn();
    const announcer = new SettleAnnouncer(1500, onSettle);
    announcer.update('a');
    await vi.advanceTimersByTimeAsync(1500);
    expect(onSettle).toHaveBeenLastCalledWith('a');

    announcer.update('');
    expect(onSettle).toHaveBeenLastCalledWith('');

    announcer.update('a'); // same value as before the clear — must announce again, not be a no-op
    expect(announcer.isPending).toBe(true);
    await vi.advanceTimersByTimeAsync(1500);
    expect(onSettle).toHaveBeenCalledTimes(3);
    expect(onSettle).toHaveBeenLastCalledWith('a');
  });

  it('fires the optional onChange synchronously for a differing, non-empty update, before settling', () => {
    vi.useFakeTimers();
    const onSettle = vi.fn();
    const onChange = vi.fn();
    const announcer = new SettleAnnouncer(1500, onSettle, onChange);
    announcer.update('a');
    expect(onChange).toHaveBeenCalledExactlyOnceWith('a');
    expect(onSettle).not.toHaveBeenCalled();
  });

  it('does not fire onChange for an unchanged value or a clear', () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    const announcer = new SettleAnnouncer(1500, vi.fn(), onChange);
    announcer.update('a');
    onChange.mockClear();
    announcer.update('a'); // unchanged
    announcer.update(''); // clear
    expect(onChange).not.toHaveBeenCalled();
  });

  it('fires onChange for every differing update, including while a timer is in flight', () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    const announcer = new SettleAnnouncer(1500, vi.fn(), onChange);
    announcer.update('a');
    announcer.update('b'); // differing while a timer is pending — must fire again, not just once
    expect(onChange).toHaveBeenCalledTimes(2);
    expect(onChange).toHaveBeenLastCalledWith('b');
  });

  it('dispose clears a pending timer so it can never fire afterward', async () => {
    vi.useFakeTimers();
    const onSettle = vi.fn();
    const announcer = new SettleAnnouncer(1500, onSettle);
    announcer.update('a');
    expect(announcer.isPending).toBe(true);

    announcer.dispose();
    expect(announcer.isPending).toBe(false);

    await vi.advanceTimersByTimeAsync(1500);
    expect(onSettle).not.toHaveBeenCalled();
  });

  it('is permanently inert after dispose — update() never re-arms a timer or fires a callback', async () => {
    vi.useFakeTimers();
    const onSettle = vi.fn();
    const onChange = vi.fn();
    const announcer = new SettleAnnouncer(1500, onSettle, onChange);
    announcer.update('a');
    announcer.dispose();

    announcer.update('b'); // a differing, non-empty value — would normally arm a fresh timer
    expect(announcer.isPending).toBe(false);
    expect(onChange).not.toHaveBeenCalledWith('b');

    await vi.advanceTimersByTimeAsync(1500);
    expect(onSettle).not.toHaveBeenCalled();
  });

  it('dispose is a no-op when nothing is pending', () => {
    const announcer = new SettleAnnouncer(1500, vi.fn());
    expect(() => announcer.dispose()).not.toThrow();
    expect(announcer.isPending).toBe(false);
  });
});
