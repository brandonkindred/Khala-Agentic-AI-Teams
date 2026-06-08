import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { pollJobStatus } from './job-status-poller';
import type { CodingTeamJobStatus } from '../models/coding-team.model';

describe('pollJobStatus', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('emits each status until terminal, then stops', () => {
    const statuses: CodingTeamJobStatus[] = [
      { job_id: 'j', status: 'running' },
      { job_id: 'j', status: 'completed' },
    ];
    let i = 0;
    const api = { getJobStatus: vi.fn(() => of(statuses[Math.min(i++, statuses.length - 1)])) };
    const seen: CodingTeamJobStatus[] = [];
    const lost = vi.fn();

    const sub = pollJobStatus(api, 'j', (s) => seen.push(s), lost);
    vi.advanceTimersByTime(5000);
    vi.advanceTimersByTime(5000);
    vi.advanceTimersByTime(5000); // no further emissions after terminal

    expect(seen.map((s) => s.status)).toEqual(['running', 'completed']);
    expect(lost).not.toHaveBeenCalled();
    expect(sub.closed).toBe(true);
  });

  it('calls onConnectionLost after the error budget is exhausted', () => {
    const api = { getJobStatus: vi.fn(() => throwError(() => new Error('boom'))) };
    const lost = vi.fn();

    const sub = pollJobStatus(api, 'j', vi.fn(), lost);
    vi.advanceTimersByTime(5000);
    vi.advanceTimersByTime(5000);
    expect(lost).not.toHaveBeenCalled(); // only 2 errors so far
    vi.advanceTimersByTime(5000);
    expect(lost).toHaveBeenCalledTimes(1); // 3rd error trips the budget
    expect(sub.closed).toBe(true); // polling stops (no leak / no further API calls)
  });
});
