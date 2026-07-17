import { vi } from 'vitest';
import {
  badgeClass,
  badgeLabel,
  isLatestRunning,
  reviewDuration,
  severityEntries,
  terminalTimestamp,
  type ReviewDurationInput,
} from './review-metrics';
import { makeReviewRecord as record } from './testing/fixtures';

describe('review-metrics', () => {
  describe('severityEntries', () => {
    it('returns only non-zero levels in critical→info order', () => {
      expect(severityEntries(undefined)).toEqual([]);
      expect(severityEntries({ total_issues: 0, inline_comments: 0, event: 'COMMENT' })).toEqual([]);
      expect(
        severityEntries({
          total_issues: 6,
          inline_comments: 0,
          event: 'REQUEST_CHANGES',
          severity_counts: { critical: 1, high: 0, medium: 2, low: 0, info: 3 },
        }),
      ).toEqual([
        { level: 'critical', count: 1 },
        { level: 'medium', count: 2 },
        { level: 'info', count: 3 },
      ]);
      expect(
        severityEntries({
          total_issues: 0,
          inline_comments: 0,
          event: 'APPROVE',
          severity_counts: { critical: 0, high: 0, medium: 0, low: 0, info: 0 },
        }),
      ).toEqual([]);
    });
  });

  describe('reviewDuration', () => {
    const base: ReviewDurationInput = { jobId: 'j', status: 'completed', startedAt: 1_000_000 };

    it('returns null for a non-terminal run or one without a completion time', () => {
      expect(reviewDuration({ ...base, status: 'running', completedAt: base.startedAt + 1000 })).toBeNull();
      expect(reviewDuration({ ...base, completedAt: undefined })).toBeNull();
    });

    it('formats seconds / minutes+seconds / hours+minutes', () => {
      expect(reviewDuration({ ...base, completedAt: base.startedAt + 45_000 })).toBe('45s');
      expect(reviewDuration({ ...base, completedAt: base.startedAt + 83_000 })).toBe('1m 23s');
      expect(
        reviewDuration({ ...base, completedAt: base.startedAt + (2 * 3600 + 5 * 60) * 1000 }),
      ).toBe('2h 5m');
    });

    it('warns and returns null on a negative (clock-skewed) interval', () => {
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
      expect(reviewDuration({ ...base, completedAt: base.startedAt - 1000 })).toBeNull();
      expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining('Negative review duration'));
      warnSpy.mockRestore();
    });
  });

  describe('terminalTimestamp', () => {
    it('prefers updated_at (the terminal transition) over last_activity_at', () => {
      expect(
        terminalTimestamp({ updated_at: '2026-03-01T00:10:00Z', last_activity_at: '2026-03-01T00:02:00Z' }),
      ).toBe(Date.parse('2026-03-01T00:10:00Z'));
    });

    it('falls back to last_activity_at when updated_at is absent', () => {
      expect(terminalTimestamp({ last_activity_at: '2026-03-02T00:00:00Z' })).toBe(
        Date.parse('2026-03-02T00:00:00Z'),
      );
    });

    it('falls back to last_activity_at when updated_at is an empty string', () => {
      expect(terminalTimestamp({ updated_at: '', last_activity_at: '2026-03-02T00:00:00Z' })).toBe(
        Date.parse('2026-03-02T00:00:00Z'),
      );
    });

    it('falls back to last_activity_at when updated_at is present but unparseable', () => {
      expect(terminalTimestamp({ updated_at: 'not-a-date', last_activity_at: '2026-03-02T00:00:00Z' })).toBe(
        Date.parse('2026-03-02T00:00:00Z'),
      );
    });

    it('falls back to Date.now() when no server timestamp is present or parseable', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-04-01T00:00:00Z'));
      expect(terminalTimestamp({})).toBe(Date.parse('2026-04-01T00:00:00Z'));
      expect(terminalTimestamp({ updated_at: 'not-a-date' })).toBe(Date.parse('2026-04-01T00:00:00Z'));
      vi.useRealTimers();
    });
  });

  describe('isLatestRunning', () => {
    it('is false when there is no latest review', () => {
      expect(isLatestRunning(null)).toBe(false);
    });

    it('is true only for a non-terminal, non-errored latest review', () => {
      expect(isLatestRunning(record({ status: 'running' }))).toBe(true);
      expect(isLatestRunning(record({ status: 'completed' }))).toBe(false);
      expect(isLatestRunning(record({ status: 'running', error: 'boom' }))).toBe(false);
    });
  });

  describe('badgeLabel', () => {
    it('is null when there is no latest review', () => {
      expect(badgeLabel(null)).toBeNull();
    });

    it('prefers an error over any status', () => {
      expect(badgeLabel(record({ status: 'running', error: 'Lost connection' }))).toBe('error');
    });

    it('shows the review-summary event when terminal, falling back to the raw status', () => {
      expect(
        badgeLabel(
          record({
            status: 'completed',
            reviewSummary: { total_issues: 0, inline_comments: 0, comment_findings: 0, event: 'COMMENT' },
          }),
        ),
      ).toBe('COMMENT');
      expect(badgeLabel(record({ status: 'completed' }))).toBe('completed'); // terminal, no summary
      expect(badgeLabel(record({ status: 'failed' }))).toBe('failed');
    });

    it('shows the raw status while still running', () => {
      expect(badgeLabel(record({ status: 'running' }))).toBe('running');
    });
  });

  describe('badgeClass', () => {
    it('is empty when there is no latest review', () => {
      expect(badgeClass(null)).toBe('');
    });

    it('is the failed class for an error or a failed status', () => {
      expect(badgeClass(record({ status: 'running', error: 'Lost connection' }))).toBe('cr-job-status--failed');
      expect(badgeClass(record({ status: 'failed' }))).toBe('cr-job-status--failed');
    });

    it('is the completed class for any other terminal status', () => {
      expect(badgeClass(record({ status: 'completed' }))).toBe('cr-job-status--completed');
    });

    it('is empty while still running', () => {
      expect(badgeClass(record({ status: 'running' }))).toBe('');
    });
  });
});
