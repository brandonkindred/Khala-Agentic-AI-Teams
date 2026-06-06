import { describe, it, expect } from 'vitest';
import {
  COMPLETED_WITH_FAILURES,
  CODING_TEAM_TERMINAL_STATUSES,
  isCodingTeamTerminalStatus,
} from './job-status.model';

describe('job-status.model', () => {
  it('partial-success constant is included in the terminal set', () => {
    expect(COMPLETED_WITH_FAILURES).toBe('completed_with_failures');
    expect(CODING_TEAM_TERMINAL_STATUSES).toContain(COMPLETED_WITH_FAILURES);
    expect(CODING_TEAM_TERMINAL_STATUSES).toEqual([
      'completed',
      'completed_with_failures',
      'failed',
      'cancelled',
    ]);
  });

  it('isCodingTeamTerminalStatus recognises every terminal status', () => {
    for (const s of CODING_TEAM_TERMINAL_STATUSES) {
      expect(isCodingTeamTerminalStatus(s)).toBe(true);
    }
  });

  it('isCodingTeamTerminalStatus is false for running/unknown/empty values', () => {
    expect(isCodingTeamTerminalStatus('running')).toBe(false);
    expect(isCodingTeamTerminalStatus('queued')).toBe(false);
    expect(isCodingTeamTerminalStatus('')).toBe(false);
    expect(isCodingTeamTerminalStatus(null)).toBe(false);
    expect(isCodingTeamTerminalStatus(undefined)).toBe(false);
  });
});
