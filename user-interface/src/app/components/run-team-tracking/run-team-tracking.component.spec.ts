import { RunTeamTrackingComponent } from './run-team-tracking.component';
import type { JobStatusResponse, TaskStateEntry } from '../../models';

describe('RunTeamTrackingComponent work tree fallback initiative behavior', () => {
  const createComponent = (): RunTeamTrackingComponent => {
    return new RunTeamTrackingComponent({} as never);
  };

  const baseStatus = (): JobStatusResponse => ({
    job_id: 'job-1',
    status: 'completed',
    task_results: [],
    task_ids: [],
    failed_tasks: [],
  });

  it('does not create fallback initiative when all items are categorized', () => {
    const component = createComponent();

    const taskStates: Record<string, TaskStateEntry> = {
      'initiative-1': { status: 'completed', assignee: 'planner', title: 'Initiative: Checkout Revamp' },
      'epic-1': {
        status: 'completed',
        assignee: 'planner',
        title: 'Epic: Payment Pipeline',
        dependencies: ['initiative-1'],
      },
      'task-1': {
        status: 'completed',
        assignee: 'backend',
        title: 'Task: Add payment API',
        dependencies: ['epic-1'],
      },
      'subtask-1': {
        status: 'completed',
        assignee: 'backend',
        title: 'Subtask: Add endpoint tests',
        dependencies: ['task-1'],
      },
    };

    const status: JobStatusResponse = {
      ...baseStatus(),
      task_ids: ['initiative-1', 'epic-1', 'task-1', 'subtask-1'],
      task_states: taskStates,
    };

    const rows = (component as never as { buildWorkTreeRows: (s: JobStatusResponse) => Array<{ label: string; status: string }> })
      .buildWorkTreeRows(status);

    expect(rows.some((row) => row.label === 'Uncategorized Initiative')).toBeFalse();
    expect(rows[0]?.status).toBe('completed');
  });

  it('creates fallback initiative only when uncategorized work exists', () => {
    const component = createComponent();

    const taskStates: Record<string, TaskStateEntry> = {
      'task-uncat': {
        status: 'in_progress',
        assignee: 'frontend',
        title: 'Task: Build cart UI',
      },
    };

    const status: JobStatusResponse = {
      ...baseStatus(),
      status: 'running',
      task_ids: ['task-uncat'],
      task_states: taskStates,
    };

    const rows = (component as never as { buildWorkTreeRows: (s: JobStatusResponse) => Array<{ label: string }> })
      .buildWorkTreeRows(status);

    expect(rows.some((row) => row.label === 'Uncategorized Initiative')).toBeTrue();
  });
});
