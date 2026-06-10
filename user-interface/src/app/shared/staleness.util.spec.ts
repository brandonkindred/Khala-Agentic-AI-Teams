import { isStalled, lastActivityAgoMs, lastActivityLabel, STALL_THRESHOLD_MS } from './staleness.util';

describe('staleness.util', () => {
  const NOW = Date.parse('2026-06-10T12:00:00Z');

  const at = (secondsAgo: number): string => new Date(NOW - secondsAgo * 1000).toISOString();

  describe('lastActivityAgoMs', () => {
    it('reads last_activity_at when present', () => {
      expect(lastActivityAgoMs({ last_activity_at: at(42) }, NOW)).toBe(42_000);
    });

    it('falls back to updated_at for older jobs', () => {
      expect(lastActivityAgoMs({ updated_at: at(10) }, NOW)).toBe(10_000);
    });

    it('prefers last_activity_at over updated_at', () => {
      expect(lastActivityAgoMs({ last_activity_at: at(300), updated_at: at(5) }, NOW)).toBe(300_000);
    });

    it('returns null when no timestamp exists', () => {
      expect(lastActivityAgoMs({}, NOW)).toBeNull();
      expect(lastActivityAgoMs(null, NOW)).toBeNull();
      expect(lastActivityAgoMs(undefined, NOW)).toBeNull();
    });

    it('returns null for unparsable timestamps', () => {
      expect(lastActivityAgoMs({ last_activity_at: 'not-a-date' }, NOW)).toBeNull();
    });

    it('clamps small clock skew to zero instead of a negative age', () => {
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

  describe('isStalled', () => {
    const stale = at((STALL_THRESHOLD_MS + 60_000) / 1000);
    const fresh = at(5);

    it('true only for a running job past the threshold', () => {
      expect(isStalled({ status: 'running', last_activity_at: stale }, NOW)).toBe(true);
    });

    it('false when activity is fresh', () => {
      expect(isStalled({ status: 'running', last_activity_at: fresh }, NOW)).toBe(false);
    });

    it('false while waiting for answers (idle-by-design)', () => {
      expect(
        isStalled({ status: 'running', waiting_for_answers: true, last_activity_at: stale }, NOW),
      ).toBe(false);
    });

    it('false on terminal or pending states', () => {
      expect(isStalled({ status: 'completed', last_activity_at: stale }, NOW)).toBe(false);
      expect(isStalled({ status: 'failed', last_activity_at: stale }, NOW)).toBe(false);
      expect(isStalled({ status: 'pending', last_activity_at: stale }, NOW)).toBe(false);
    });

    it('false when there is no timestamp to judge by', () => {
      expect(isStalled({ status: 'running' }, NOW)).toBe(false);
      expect(isStalled(null, NOW)).toBe(false);
    });
  });
});
