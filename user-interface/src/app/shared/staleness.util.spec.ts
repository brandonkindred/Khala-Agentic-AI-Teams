import {
  isStalled,
  lastActivityAgoMs,
  lastActivityDurationLabel,
  lastActivityLabel,
  STALL_THRESHOLD_MS,
} from './staleness.util';

describe('staleness.util', () => {
  const NOW = Date.parse('2026-06-10T12:00:00Z');

  const at = (secondsAgo: number): string => new Date(NOW - secondsAgo * 1000).toISOString();

  describe('lastActivityAgoMs', () => {
    it('reads last_activity_at when present', () => {
      expect(lastActivityAgoMs({ last_activity_at: at(42) }, NOW)).toBe(42_000);
    });

    it('never falls back to updated_at — the heartbeat refreshes it even when the orchestrator is hung', () => {
      // A dead job whose heartbeat keeps updated_at fresh must read as "unknown",
      // not as freshly active: false reassurance suppresses the stall warning.
      expect(lastActivityAgoMs({ updated_at: at(10) } as never, NOW)).toBeNull();
    });

    it('returns null when no timestamp exists', () => {
      expect(lastActivityAgoMs({}, NOW)).toBeNull();
      expect(lastActivityAgoMs(null, NOW)).toBeNull();
      expect(lastActivityAgoMs(undefined, NOW)).toBeNull();
    });

    it('returns null for unparsable timestamps', () => {
      expect(lastActivityAgoMs({ last_activity_at: 'not-a-date' }, NOW)).toBeNull();
    });

    it('computes the age against server_time when present (browser clock skew immunity)', () => {
      // Browser clock 30 minutes ahead of the backend: with server_time the age
      // is the true 60s; without it the job would look stalled on every poll.
      const browserNow = NOW + 30 * 60_000;
      const status = { last_activity_at: at(60), server_time: new Date(NOW).toISOString() };
      expect(lastActivityAgoMs(status, browserNow)).toBe(60_000);
    });

    it('falls back to the browser clock when server_time is absent or unparsable', () => {
      expect(lastActivityAgoMs({ last_activity_at: at(42), server_time: 'garbage' }, NOW)).toBe(42_000);
      expect(lastActivityAgoMs({ last_activity_at: at(42), server_time: null }, NOW)).toBe(42_000);
    });

    it('clamps small negative ages to zero instead of a bogus future age', () => {
      expect(lastActivityAgoMs({ last_activity_at: at(-5) }, NOW)).toBe(0);
    });
  });

  describe('lastActivityLabel', () => {
    it('formats just now / seconds / minutes / hours', () => {
      expect(lastActivityLabel({ last_activity_at: at(3) }, NOW)).toBe('just now');
      expect(lastActivityLabel({ last_activity_at: at(42) }, NOW)).toBe('42s ago');
      expect(lastActivityLabel({ last_activity_at: at(3 * 60) }, NOW)).toBe('3m ago');
      expect(lastActivityLabel({ last_activity_at: at(2 * 3600) }, NOW)).toBe('2h ago');
    });

    it('returns empty string when no timestamp exists', () => {
      expect(lastActivityLabel({}, NOW)).toBe('');
    });
  });

  describe('lastActivityDurationLabel', () => {
    it('formats a suffix-free duration for embedding in sentences', () => {
      expect(lastActivityDurationLabel({ last_activity_at: at(42) }, NOW)).toBe('42s');
      expect(lastActivityDurationLabel({ last_activity_at: at(12 * 60) }, NOW)).toBe('12m');
      expect(lastActivityDurationLabel({ last_activity_at: at(2 * 3600) }, NOW)).toBe('2h');
    });

    it('returns empty string when no timestamp exists', () => {
      expect(lastActivityDurationLabel({}, NOW)).toBe('');
    });
  });

  describe('isStalled', () => {
    const stale = at((STALL_THRESHOLD_MS + 60_000) / 1000);
    const fresh = at(5);

    it('tolerates multi-minute LLM calls and the 300s rate-limit backoff', () => {
      // A healthy single LLM call can run 5-10 minutes with no job write; the
      // threshold must exceed the client's 300s internal 429 backoff.
      expect(STALL_THRESHOLD_MS).toBeGreaterThan(300_000);
    });

    it('true for a running job past the threshold', () => {
      expect(isStalled({ status: 'running', last_activity_at: stale }, NOW)).toBe(true);
    });

    it('true for a pending job past the threshold (hang before the first status write)', () => {
      expect(isStalled({ status: 'pending', last_activity_at: stale }, NOW)).toBe(true);
    });

    it('false when activity is fresh', () => {
      expect(isStalled({ status: 'running', last_activity_at: fresh }, NOW)).toBe(false);
    });

    it('false while waiting for answers (idle-by-design)', () => {
      expect(
        isStalled({ status: 'running', waiting_for_answers: true, last_activity_at: stale }, NOW),
      ).toBe(false);
    });

    it('false on terminal states', () => {
      expect(isStalled({ status: 'completed', last_activity_at: stale }, NOW)).toBe(false);
      expect(isStalled({ status: 'completed_with_failures', last_activity_at: stale }, NOW)).toBe(false);
      expect(isStalled({ status: 'failed', last_activity_at: stale }, NOW)).toBe(false);
    });

    it('false when there is no timestamp to judge by', () => {
      expect(isStalled({ status: 'running' }, NOW)).toBe(false);
      expect(isStalled(null, NOW)).toBe(false);
    });

    it('never fooled by a browser clock ahead of the backend', () => {
      const browserNow = NOW + STALL_THRESHOLD_MS + 120_000;
      const status = {
        status: 'running',
        last_activity_at: fresh,
        server_time: new Date(NOW).toISOString(),
      };
      expect(isStalled(status, browserNow)).toBe(false);
    });
  });
});
