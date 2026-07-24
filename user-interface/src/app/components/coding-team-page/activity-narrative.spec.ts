import { describe, expect, it } from 'vitest';
import type { CodingTeamJobStatus } from '../../models/coding-team.model';
import {
  ACTIVITY_NARRATIVE_MAX_LINES,
  appendActivityNarrative,
  emptyActivityNarrative,
  thoughtStreamPanelTitle,
} from './activity-narrative';

function status(overrides: Partial<CodingTeamJobStatus> = {}): CodingTeamJobStatus {
  return { job_id: 'j1', status: 'running', ...overrides };
}

describe('activity-narrative', () => {
  it('emptyActivityNarrative starts with no lines and empty fingerprint', () => {
    const s = emptyActivityNarrative();
    expect(s.lines).toEqual([]);
    expect(s.fingerprint).toBe('');
  });

  it('appendActivityNarrative records phase and status_text changes', () => {
    let state = emptyActivityNarrative();
    state = appendActivityNarrative(
      state,
      status({ phase: 'task_graph', status_text: 'Building task graph', updated_at: '2026-07-24T15:00:00Z' }),
      '2026-07-24T15:00:01Z',
    );
    expect(state.lines.map((l) => l.text)).toEqual([
      'Phase → task_graph',
      'Status: Building task graph',
    ]);
    expect(state.lines[0].at).toBe('2026-07-24T15:00:00Z');

    const unchanged = appendActivityNarrative(
      state,
      status({ phase: 'task_graph', status_text: 'Building task graph', updated_at: '2026-07-24T15:00:02Z' }),
      '2026-07-24T15:00:03Z',
    );
    expect(unchanged.lines).toHaveLength(2);
    expect(unchanged).toBe(state); // same reference when fingerprint unchanged
  });

  it('appendActivityNarrative records current_activity and per-agent step changes', () => {
    let state = emptyActivityNarrative();
    state = appendActivityNarrative(
      state,
      status({
        current_activity: { agent: 'code_review', detail: 'chunk 1/3' },
        agents: [
          {
            agent_id: 'backend',
            role: 'implementation_worker',
            display_name: 'Backend',
            stack: 'backend',
            tools_services: [],
            status: 'working',
            current_task_id: 't1',
            current_task_title: 'Add API',
            current_step: 'implementing',
            activity_detail: null,
            activity_fraction: null,
          },
        ],
        last_activity_at: '2026-07-24T15:01:00Z',
      }),
      '2026-07-24T15:01:01Z',
    );
    expect(state.lines.map((l) => l.text)).toEqual([
      'Activity: code_review — chunk 1/3',
      'Agent Backend: Add API — implementing',
    ]);
  });

  it('caps narrative at ACTIVITY_NARRATIVE_MAX_LINES newest lines', () => {
    let state = emptyActivityNarrative();
    for (let i = 0; i < ACTIVITY_NARRATIVE_MAX_LINES + 5; i++) {
      state = appendActivityNarrative(
        state,
        status({ status_text: `tick ${i}`, updated_at: `2026-07-24T15:00:${String(i % 60).padStart(2, '0')}Z` }),
        `2026-07-24T15:00:${String(i % 60).padStart(2, '0')}Z`,
      );
    }
    expect(state.lines).toHaveLength(ACTIVITY_NARRATIVE_MAX_LINES);
    expect(state.lines[0].text).toBe(`Status: tick 5`);
    expect(state.lines.at(-1)?.text).toBe(`Status: tick ${ACTIVITY_NARRATIVE_MAX_LINES + 4}`);
  });

  it('thoughtStreamPanelTitle prefers thinking title when reasoning is present', () => {
    expect(thoughtStreamPanelTitle(true, false)).toBe('Agent thinking');
    expect(thoughtStreamPanelTitle(true, true)).toBe('Agent thinking');
    expect(thoughtStreamPanelTitle(false, true)).toBe('Agent activity');
    expect(thoughtStreamPanelTitle(false, false)).toBeNull();
  });
});
