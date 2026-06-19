import { describe, it, expect } from 'vitest';
import {
  ALREADY_COMPLETE,
  COMPLETED_WITH_FAILURES,
  CODING_TEAM_TERMINAL_STATUSES,
  isCodingTeamTerminalStatus,
} from './job-status.model';

describe('job-status.model', () => {
  it('partial-success and already-complete constants are included in the terminal set', () => {
    expect(COMPLETED_WITH_FAILURES).toBe('completed_with_failures');
    expect(ALREADY_COMPLETE).toBe('already_complete');
    expect(CODING_TEAM_TERMINAL_STATUSES).toContain(COMPLETED_WITH_FAILURES);
    expect(CODING_TEAM_TERMINAL_STATUSES).toContain(ALREADY_COMPLETE);
    expect(CODING_TEAM_TERMINAL_STATUSES).toEqual([
      'completed',
      'completed_with_failures',
      'already_complete',
      'failed',
      'cancelled',
    ]);
  });

  it('already_complete is recognised as a terminal status (poller stops, run renders finished)', () => {
    expect(isCodingTeamTerminalStatus('already_complete')).toBe(true);
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
