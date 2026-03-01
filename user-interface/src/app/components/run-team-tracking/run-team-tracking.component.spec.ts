import { RunTeamTrackingComponent } from './run-team-tracking.component';
import type { JobStatusResponse, TaskStateEntry, PlanningHierarchy } from '../../models';

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

  // Legacy tests for text-pattern matching (backward compatibility)
  describe('legacy text-pattern classification (no planning_hierarchy)', () => {
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

  // New tests for hierarchy-based tree building
  describe('hierarchy-based tree building (with planning_hierarchy)', () => {
    it('builds tree from planning_hierarchy data', () => {
      const component = createComponent();

      const hierarchy: PlanningHierarchy = {
        initiatives: [
          { id: 'init-1', title: 'Core Task Management', description: 'Main initiative' },
        ],
        epics: [
          { id: 'epic-1', title: 'Task CRUD Operations', description: 'Create, read, update, delete tasks', initiative_id: 'init-1' },
        ],
        stories: [
          { id: 'story-1', title: 'Create Task API', description: 'Backend API for creating tasks', epic_id: 'epic-1', initiative_id: 'init-1' },
        ],
      };

      const taskStates: Record<string, TaskStateEntry> = {
        'task-1': {
          status: 'completed',
          assignee: 'backend',
          title: 'Implement POST /tasks endpoint',
          initiative_id: 'init-1',
          epic_id: 'epic-1',
          story_id: 'story-1',
        },
        'task-2': {
          status: 'in_progress',
          assignee: 'backend',
          title: 'Add validation middleware',
          initiative_id: 'init-1',
          epic_id: 'epic-1',
          story_id: 'story-1',
        },
      };

      const status: JobStatusResponse = {
        ...baseStatus(),
        status: 'running',
        task_ids: ['task-1', 'task-2'],
        task_states: taskStates,
        planning_hierarchy: hierarchy,
      };

      const rows = (component as never as { buildWorkTreeRows: (s: JobStatusResponse) => Array<{ label: string; level: string }> })
        .buildWorkTreeRows(status);

      // Should have proper hierarchy labels from planning_hierarchy
      expect(rows.some((row) => row.label === 'Core Task Management')).toBeTrue();
      expect(rows.some((row) => row.label === 'Task CRUD Operations')).toBeTrue();
      expect(rows.some((row) => row.label === 'Create Task API')).toBeTrue();
      // Should NOT have fallback labels
      expect(rows.some((row) => row.label === 'Uncategorized Initiative')).toBeFalse();
      expect(rows.some((row) => row.label === 'General Epic')).toBeFalse();
    });

    it('places orphan tasks in fallback when hierarchy metadata is missing', () => {
      const component = createComponent();

      const hierarchy: PlanningHierarchy = {
        initiatives: [
          { id: 'init-1', title: 'Core Task Management', description: '' },
        ],
        epics: [],
        stories: [],
      };

      const taskStates: Record<string, TaskStateEntry> = {
        'task-orphan': {
          status: 'pending',
          assignee: 'backend',
          title: 'Orphan Task Without Parents',
        },
      };

      const status: JobStatusResponse = {
        ...baseStatus(),
        task_ids: ['task-orphan'],
        task_states: taskStates,
        planning_hierarchy: hierarchy,
      };

      const rows = (component as never as { buildWorkTreeRows: (s: JobStatusResponse) => Array<{ label: string }> })
        .buildWorkTreeRows(status);

      // Orphan tasks should go into fallback
      expect(rows.some((row) => row.label === 'Uncategorized Initiative')).toBeTrue();
      expect(rows.some((row) => row.label === 'General Epic')).toBeTrue();
    });

    it('derives status from children correctly', () => {
      const component = createComponent();

      const hierarchy: PlanningHierarchy = {
        initiatives: [
          { id: 'init-1', title: 'Initiative One', description: '' },
        ],
        epics: [
          { id: 'epic-1', title: 'Epic One', description: '', initiative_id: 'init-1' },
        ],
        stories: [
          { id: 'story-1', title: 'Story One', description: '', epic_id: 'epic-1', initiative_id: 'init-1' },
        ],
      };

      const taskStates: Record<string, TaskStateEntry> = {
        'task-1': { status: 'done', assignee: 'backend', title: 'Task 1', story_id: 'story-1', epic_id: 'epic-1', initiative_id: 'init-1' },
        'task-2': { status: 'done', assignee: 'backend', title: 'Task 2', story_id: 'story-1', epic_id: 'epic-1', initiative_id: 'init-1' },
      };

      const status: JobStatusResponse = {
        ...baseStatus(),
        status: 'completed',
        task_ids: ['task-1', 'task-2'],
        task_states: taskStates,
        planning_hierarchy: hierarchy,
      };

      const rows = (component as never as { buildWorkTreeRows: (s: JobStatusResponse) => Array<{ label: string; status: string }> })
        .buildWorkTreeRows(status);

      // All tasks completed, so parent statuses should also be completed
      const initiativeRow = rows.find((row) => row.label === 'Initiative One');
      expect(initiativeRow?.status).toBe('completed');
    });
  });
});
