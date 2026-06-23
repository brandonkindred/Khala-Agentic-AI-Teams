import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { of, throwError } from 'rxjs';
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

  it('keeps polling after a transient fetch error (default onError:continue)', () => {
    const results = [throwError(() => new Error('boom')), of('running'), of('completed')];
    let i = 0;
    const emitted: string[] = [];
    let completed = false;
    let errored = false;

    const sub = pollWhile(
      () => results[Math.min(i++, results.length - 1)],
      (s) => s === 'completed',
      { intervalMs: 100 },
    ).subscribe({
      next: (v) => emitted.push(v),
      complete: () => (completed = true),
      error: () => (errored = true),
    });

    vi.advanceTimersByTime(0); // first poll errors → swallowed, no emission
    expect(emitted).toEqual([]);
    expect(errored).toBe(false);
    vi.advanceTimersByTime(100);
    expect(emitted).toEqual(['running']);
    vi.advanceTimersByTime(100);
    expect(emitted).toEqual(['running', 'completed']);
    expect(completed).toBe(true);
    expect(errored).toBe(false);
    sub.unsubscribe();
  });

  it('keeps polling through multiple consecutive errors (default onError:continue)', () => {
    const results = [
      throwError(() => new Error('boom1')),
      throwError(() => new Error('boom2')),
      throwError(() => new Error('boom3')),
      of('completed'),
    ];
    let i = 0;
    const emitted: string[] = [];
    let completed = false;
    let errored = false;

    pollWhile(() => results[Math.min(i++, results.length - 1)], (s) => s === 'completed', {
      intervalMs: 100,
    }).subscribe({
      next: (v) => emitted.push(v),
      complete: () => (completed = true),
      error: () => (errored = true),
    });

    vi.advanceTimersByTime(0); // error 1 — swallowed
    vi.advanceTimersByTime(100); // error 2 — swallowed
    vi.advanceTimersByTime(100); // error 3 — swallowed
    expect(emitted).toEqual([]);
    expect(errored).toBe(false);
    vi.advanceTimersByTime(100); // success
    expect(emitted).toEqual(['completed']);
    expect(completed).toBe(true);
    expect(errored).toBe(false);
  });

  it('onError:stop propagates the error and terminates the stream', () => {
    let errored = false;
    let completed = false;
    pollWhile(() => throwError(() => new Error('boom')), () => false, {
      intervalMs: 100,
      onError: 'stop',
    }).subscribe({ error: () => (errored = true), complete: () => (completed = true) });

    vi.advanceTimersByTime(0);
    expect(errored).toBe(true);
    expect(completed).toBe(false);
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
