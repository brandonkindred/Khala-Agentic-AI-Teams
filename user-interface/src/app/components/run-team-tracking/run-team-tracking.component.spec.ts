import { TestBed } from '@angular/core/testing';
import { Subject } from 'rxjs';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { RunTeamTrackingComponent } from './run-team-tracking.component';
import type { JobStatusResponse, TaskStateEntry, PlanningHierarchy } from '../../models';

// Make timer fire once synchronously so the poll callback runs in the test without a real interval.
vi.mock('rxjs', async (importOriginal) => {
  const rxjs = await importOriginal<typeof import('rxjs')>();
  return { ...rxjs, timer: vi.fn(() => rxjs.of(0)) };
});

describe('RunTeamTrackingComponent work tree fallback initiative behavior', () => {
  const createComponent = (): RunTeamTrackingComponent => {
    const apiSpy = { getJobStatus: vi.fn() };
    TestBed.configureTestingModule({
      providers: [{ provide: SoftwareEngineeringApiService, useValue: apiSpy }],
    });
    return TestBed.runInInjectionContext(() => new RunTeamTrackingComponent());
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

      const rows = (component as never as { buildWorkTreeRows: (s: JobStatusResponse) => { label: string; status: string }[] })
        .buildWorkTreeRows(status);

      expect(rows.some((row) => row.label === 'Uncategorized Initiative')).toBe(false);
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

      const rows = (component as never as { buildWorkTreeRows: (s: JobStatusResponse) => { label: string }[] })
        .buildWorkTreeRows(status);

      expect(rows.some((row) => row.label === 'Uncategorized Initiative')).toBe(true);
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

      const rows = (component as never as { buildWorkTreeRows: (s: JobStatusResponse) => { label: string; level: string }[] })
        .buildWorkTreeRows(status);

      // Should have proper hierarchy labels from planning_hierarchy
      expect(rows.some((row) => row.label === 'Core Task Management')).toBe(true);
      expect(rows.some((row) => row.label === 'Task CRUD Operations')).toBe(true);
      expect(rows.some((row) => row.label === 'Create Task API')).toBe(true);
      // Should NOT have fallback labels
      expect(rows.some((row) => row.label === 'Uncategorized Initiative')).toBe(false);
      expect(rows.some((row) => row.label === 'General Epic')).toBe(false);
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

      const rows = (component as never as { buildWorkTreeRows: (s: JobStatusResponse) => { label: string }[] })
        .buildWorkTreeRows(status);

      // Orphan tasks should go into fallback
      expect(rows.some((row) => row.label === 'Uncategorized Initiative')).toBe(true);
      expect(rows.some((row) => row.label === 'General Epic')).toBe(true);
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

      const rows = (component as never as { buildWorkTreeRows: (s: JobStatusResponse) => { label: string; status: string }[] })
        .buildWorkTreeRows(status);

      // All tasks completed, so parent statuses should also be completed
      const initiativeRow = rows.find((row) => row.label === 'Initiative One');
      expect(initiativeRow?.status).toBe('completed');
    });
  });
});

describe('RunTeamTrackingComponent derived-value memoization', () => {
  const createComponent = (): RunTeamTrackingComponent => {
    const apiSpy = { getJobStatus: vi.fn() };
    TestBed.configureTestingModule({
      providers: [{ provide: SoftwareEngineeringApiService, useValue: apiSpy }],
    });
    return TestBed.runInInjectionContext(() => new RunTeamTrackingComponent());
  };

  const statusWithTasks = (): JobStatusResponse => ({
    job_id: 'job-1',
    status: 'running',
    task_results: [],
    failed_tasks: [],
    task_ids: ['t1'],
    task_states: { t1: { status: 'in_progress', assignee: 'backend', title: 'T1' } },
  });

  it('caches getTeamsWithTasks/buildDAGTree per status and rebuilds on a new status', () => {
    const component = createComponent();
    component.status = statusWithTasks();

    const teams1 = component.getTeamsWithTasks();
    expect(component.getTeamsWithTasks()).toBe(teams1); // same reference: rebuilt only once per status
    const dag1 = component.buildDAGTree();
    expect(component.buildDAGTree()).toBe(dag1);

    // A poll delivers a new status object — the cache must invalidate and rebuild.
    component.status = statusWithTasks();
    expect(component.getTeamsWithTasks()).not.toBe(teams1);
    expect(component.buildDAGTree()).not.toBe(dag1);
  });
});

describe('RunTeamTrackingComponent polling stop conditions', () => {
  it('stops polling on already_complete (a coding-team terminal success)', () => {
    // A Subject (not of()) emits AFTER ngOnInit assigns pollSub, so the stop path nulls the real
    // field rather than racing the synchronous subscription assignment.
    const statusSubject = new Subject<JobStatusResponse>();
    const apiSpy = { getJobStatus: vi.fn().mockReturnValue(statusSubject) };
    TestBed.configureTestingModule({
      providers: [{ provide: SoftwareEngineeringApiService, useValue: apiSpy }],
    });
    const component = TestBed.runInInjectionContext(() => new RunTeamTrackingComponent());
    component.jobId = 'job-1';
    component.ngOnInit();
    statusSubject.next({
      job_id: 'job-1',
      status: 'already_complete',
      task_results: [],
      task_ids: [],
      failed_tasks: [],
    });

    expect(component.status?.status).toBe('already_complete');
    // Routed through isCodingTeamTerminalStatus, so the poll unsubscribed on this terminal success
    // (a missing case here would leave the poll running forever).
    expect((component as unknown as { pollSub: unknown }).pollSub).toBeNull();
  });
});
