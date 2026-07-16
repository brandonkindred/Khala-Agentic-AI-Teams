import { vi } from 'vitest';
import {
  commentFindings,
  findingChips,
  reviewDuration,
  severityEntries,
  terminalTimestamp,
  type ReviewDurationInput,
} from './review-metrics';

describe('review-metrics', () => {
  describe('commentFindings', () => {
    it('prefers comment_findings, falls back to body_findings, then 0', () => {
      expect(
        commentFindings({ total_issues: 0, inline_comments: 0, comment_findings: 2, event: 'COMMENT' }),
      ).toBe(2);
      expect(
        commentFindings({ total_issues: 0, inline_comments: 0, body_findings: 4, event: 'COMMENT' }),
      ).toBe(4);
      expect(commentFindings({ total_issues: 0, inline_comments: 0, event: 'COMMENT' })).toBe(0);
    });
  });

  describe('findingChips', () => {
    it('builds total / inline / comments labels', () => {
      expect(
        findingChips({ total_issues: 3, inline_comments: 2, comment_findings: 1, event: 'COMMENT' }),
      ).toEqual(['3 finding(s)', '2 inline', '1 comments']);
      // The legacy body_findings key folds into the comments count.
      expect(
        findingChips({ total_issues: 0, inline_comments: 0, body_findings: 4, event: 'COMMENT' }),
      ).toEqual(['0 finding(s)', '0 inline', '4 comments']);
    });
  });

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

    it('falls back to Date.now() when no server timestamp is present or parseable', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-04-01T00:00:00Z'));
      expect(terminalTimestamp({})).toBe(Date.parse('2026-04-01T00:00:00Z'));
      expect(terminalTimestamp({ updated_at: 'not-a-date' })).toBe(Date.parse('2026-04-01T00:00:00Z'));
      vi.useRealTimers();
    });
  });
});
