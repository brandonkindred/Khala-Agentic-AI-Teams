import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { of } from 'rxjs';
import { pollWhile } from './poll-while';

describe('pollWhile', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it('polls on an interval until terminal, then completes', () => {
    const statuses = ['running', 'running', 'completed'];
    let i = 0;
    const emitted: string[] = [];
    let completed = false;

    const sub = pollWhile(
      () => of(statuses[Math.min(i++, statuses.length - 1)]),
      (s) => s === 'completed',
      { intervalMs: 100 },
    ).subscribe({ next: (v) => emitted.push(v), complete: () => (completed = true) });

    vi.advanceTimersByTime(0); // immediate first poll
    expect(emitted).toEqual(['running']);
    vi.advanceTimersByTime(100);
    expect(emitted).toEqual(['running', 'running']);
    vi.advanceTimersByTime(100);
    expect(emitted).toEqual(['running', 'running', 'completed']);
    expect(completed).toBe(true);

    // No further polls after completion.
    vi.advanceTimersByTime(1000);
    expect(emitted).toEqual(['running', 'running', 'completed']);
    sub.unsubscribe();
  });

  it('respects immediate:false (waits one interval before the first poll)', () => {
    const emitted: number[] = [];
    const sub = pollWhile(
      () => of(1),
      () => true,
      { intervalMs: 50, immediate: false },
    ).subscribe((v) => emitted.push(v));

    vi.advanceTimersByTime(0);
    expect(emitted).toEqual([]); // not yet
    vi.advanceTimersByTime(50);
    expect(emitted).toEqual([1]);
    sub.unsubscribe();
  });

  it('completes immediately when the first result is already terminal', () => {
    const emitted: string[] = [];
    let completed = false;
    pollWhile(
      () => of('done'),
      (s) => s === 'done',
    ).subscribe({ next: (v) => emitted.push(v), complete: () => (completed = true) });

    vi.advanceTimersByTime(0);
    expect(emitted).toEqual(['done']);
    expect(completed).toBe(true);
  });
});
