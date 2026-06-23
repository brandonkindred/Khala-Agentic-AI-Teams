import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { vi, beforeEach, afterEach } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { RunTeamTrackingComponent } from './run-team-tracking.component';
import type { JobStatusResponse } from '../../models';

interface ApiStub {
  getJobStatus: ReturnType<typeof vi.fn>;
}

const buildStatus = (overrides: Partial<JobStatusResponse> = {}): JobStatusResponse => ({
  job_id: 'job-1',
  status: 'running',
  task_results: [],
  task_ids: [],
  failed_tasks: [],
  ...overrides,
});

describe('RunTeamTrackingComponent (polling lifecycle & view-model helpers)', () => {
  let api: ApiStub;
  let fixture: ComponentFixture<RunTeamTrackingComponent>;
  let component: RunTeamTrackingComponent;

  beforeEach(() => {
    api = { getJobStatus: vi.fn() };
    TestBed.configureTestingModule({
      providers: [{ provide: SoftwareEngineeringApiService, useValue: api }],
    });
    fixture = TestBed.createComponent(RunTeamTrackingComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // -----------------------------------------------------------------------
  // Lifecycle and polling
  // -----------------------------------------------------------------------

  it('does not poll when jobId is null on init', () => {
    component.jobId = null;
    fixture.detectChanges();
    expect(component.loading).toBe(false);
    expect(api.getJobStatus).not.toHaveBeenCalled();
  });

  it('precomputes phaseStatuses to match the per-phase helpers after a poll', async () => {
    vi.useFakeTimers();
    api.getJobStatus.mockReturnValue(of(buildStatus({ status: 'running', phase: 'planning' })));
    component.jobId = 'job-1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);

    // The precomputed map must agree with the methods it replaces in the template.
    for (const phase of component.ALL_PHASES) {
      expect(component.phaseStatuses[phase.id]).toEqual({
        completed: component.isPhaseCompleted(phase.id),
        current: component.isCurrentPhase(phase.id),
        pending: component.isPhasePending(phase.id),
      });
    }
    expect(component.phaseStatuses['planning'].current).toBe(true);
    expect(component.phaseStatuses['product_analysis'].completed).toBe(true);
    expect(component.phaseStatuses['execution'].pending).toBe(true);
  });

  it('starts polling on init when jobId is set', async () => {
    vi.useFakeTimers();
    api.getJobStatus.mockReturnValue(of(buildStatus({ status: 'completed' })));
    component.jobId = 'job-1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(api.getJobStatus).toHaveBeenCalledWith('job-1');
    expect(component.status?.status).toBe('completed');
    expect(component.loading).toBe(false);
  });

  it('emits statusChange and unsubscribes on terminal status', async () => {
    vi.useFakeTimers();
    const emitted: JobStatusResponse[] = [];
    api.getJobStatus.mockReturnValue(of(buildStatus({ status: 'failed' })));
    component.statusChange.subscribe((s) => emitted.push(s));
    component.jobId = 'job-1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(emitted.length).toBe(1);
    expect(emitted[0].status).toBe('failed');
    expect(component['pollSub']).toBeNull();
  });

  it('stops polling on cancelled status', async () => {
    vi.useFakeTimers();
    api.getJobStatus.mockReturnValue(of(buildStatus({ status: 'cancelled' })));
    component.jobId = 'job-1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component['pollSub']).toBeNull();
  });

  it('handles polling error gracefully', async () => {
    vi.useFakeTimers();
    api.getJobStatus.mockReturnValue(throwError(() => new Error('network')));
    component.jobId = 'job-1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component.loading).toBe(false);
    expect(component['pollSub']).toBeNull();
  });

  it('reacts to jobId changes (start polling for new id)', async () => {
    vi.useFakeTimers();
    api.getJobStatus.mockReturnValue(of(buildStatus({ status: 'running' })));
    component.jobId = null;
    fixture.detectChanges();
    component.jobId = 'job-new';
    component.ngOnChanges({
      jobId: { previousValue: null, currentValue: 'job-new', firstChange: false, isFirstChange: () => false },
    });
    await vi.advanceTimersByTimeAsync(1);
    expect(api.getJobStatus).toHaveBeenCalledWith('job-new');
  });

  it('clears state and stops polling when jobId is set to null in ngOnChanges', async () => {
    vi.useFakeTimers();
    api.getJobStatus.mockReturnValue(of(buildStatus({ status: 'running' })));
    component.jobId = 'job-1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    component.jobId = null;
    component.ngOnChanges({
      jobId: { previousValue: 'job-1', currentValue: null, firstChange: false, isFirstChange: () => false },
    });
    expect(component.status).toBeNull();
    expect(component.workTreeRows).toEqual([]);
    expect(component.loading).toBe(false);
  });

  it('ignores non-jobId firstChange in ngOnChanges', () => {
    component.ngOnChanges({});
    // No exceptions, no API calls.
    expect(api.getJobStatus).not.toHaveBeenCalled();
  });

  it('restarts polling when waiting_for_answers transition flips', async () => {
    vi.useFakeTimers();
    // Each subscription gets a fresh "running, not waiting" response first, then on a
    // restart due to waiting flip, return "waiting" - we just verify multiple subscriptions occurred.
    const responses = [
      buildStatus({ status: 'running', waiting_for_answers: false }),
      buildStatus({ status: 'running', waiting_for_answers: true }),
      buildStatus({ status: 'running', waiting_for_answers: true }),
    ];
    let callIdx = 0;
    api.getJobStatus.mockImplementation(() => of(responses[Math.min(callIdx++, responses.length - 1)]));
    component.jobId = 'job-1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    // The first emit was "not waiting"; second poll cycle returns "waiting" which flips state.
    await vi.advanceTimersByTimeAsync(15000);
    expect(api.getJobStatus.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('cleans up on destroy', async () => {
    vi.useFakeTimers();
    api.getJobStatus.mockReturnValue(of(buildStatus({ status: 'running' })));
    component.jobId = 'job-1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    const sub = component['pollSub'];
    expect(sub).toBeTruthy();
    const spy = vi.spyOn(sub!, 'unsubscribe');
    component.ngOnDestroy();
    expect(spy).toHaveBeenCalled();
  });

  // -----------------------------------------------------------------------
  // Phase stepper / status badges
  // -----------------------------------------------------------------------

  it('treats coding/task_graph phases as execution for the stepper', () => {
    component.status = buildStatus({ phase: 'task_graph' });
    expect(component.isCurrentPhase('execution')).toBe(true);
    component.status = buildStatus({ phase: 'coding' });
    expect(component.isCurrentPhase('execution')).toBe(true);
  });

  it('isPhaseCompleted returns false for unknown phase id', () => {
    component.status = buildStatus({ phase: 'planning' });
    expect(component.isPhaseCompleted('nonexistent_phase')).toBe(false);
  });

  it('isPhaseCompleted true for all phases when completed', () => {
    component.status = buildStatus({ phase: 'completed' });
    expect(component.isPhaseCompleted('product_analysis')).toBe(true);
    expect(component.isPhaseCompleted('planning')).toBe(true);
    expect(component.isPhaseCompleted('execution')).toBe(true);
  });

  it('isPhaseCompleted: earlier phases done when later phase active', () => {
    component.status = buildStatus({ phase: 'execution' });
    expect(component.isPhaseCompleted('product_analysis')).toBe(true);
    expect(component.isPhaseCompleted('planning')).toBe(true);
    expect(component.isPhaseCompleted('execution')).toBe(false);
  });

  it('isPhasePending true when phase is in the future', () => {
    component.status = buildStatus({ phase: 'planning' });
    expect(component.isPhasePending('execution')).toBe(true);
    expect(component.isPhasePending('completed')).toBe(true);
    expect(component.isPhasePending('planning')).toBe(false);
  });

  it('getStatusBadge returns waiting label when waiting_for_answers', () => {
    component.status = buildStatus({ waiting_for_answers: true });
    expect(component.getStatusBadge()).toBe('Waiting for answers');
  });

  it('getStatusBadge returns status string', () => {
    component.status = buildStatus({ status: 'running' });
    expect(component.getStatusBadge()).toBe('running');
    component.status = null;
    expect(component.getStatusBadge()).toBe('pending');
  });

  it('getStatusBadgeClass maps each status', () => {
    component.status = buildStatus({ waiting_for_answers: true });
    expect(component.getStatusBadgeClass()).toBe('status-waiting');
    component.status = buildStatus({ status: 'completed' });
    expect(component.getStatusBadgeClass()).toBe('status-completed');
    component.status = buildStatus({ status: 'failed' });
    expect(component.getStatusBadgeClass()).toBe('status-failed');
    component.status = buildStatus({ status: 'running' });
    expect(component.getStatusBadgeClass()).toBe('status-running');
    component.status = buildStatus({ status: 'unknown' });
    expect(component.getStatusBadgeClass()).toBe('status-pending');
  });

  // -----------------------------------------------------------------------
  // Team lanes / tasks
  // -----------------------------------------------------------------------

  it('getTeamsWithTasks returns empty when no task_states', () => {
    component.status = buildStatus({ task_ids: ['t1'] });
    expect(component.getTeamsWithTasks()).toEqual([]);
  });

  it('getTeamsWithTasks returns empty when no task_ids', () => {
    component.status = buildStatus({ task_states: {} });
    expect(component.getTeamsWithTasks()).toEqual([]);
  });

  it('getTeamsWithTasks groups tasks by team and uses TEAM_ORDER', () => {
    component.status = buildStatus({
      task_ids: ['t1', 't2', 't3', 't4'],
      task_states: {
        t1: { status: 'done', assignee: 'frontend', title: 'A' },
        t2: { status: 'done', assignee: 'backend', title: 'B' },
        t3: { status: 'in_progress', assignee: 'devops', title: 'C' },
        t4: { status: 'pending', assignee: 'mystery-team', title: 'D' },
      },
    });
    const result = component.getTeamsWithTasks();
    const ids = result.map((r) => r.teamId);
    // devops appears before backend before frontend per TEAM_ORDER
    expect(ids.indexOf('devops')).toBeLessThan(ids.indexOf('backend'));
    expect(ids.indexOf('backend')).toBeLessThan(ids.indexOf('frontend'));
    // unknown team appended after
    expect(ids).toContain('mystery-team');
    expect(ids.indexOf('mystery-team')).toBeGreaterThan(ids.indexOf('frontend'));
  });

  it('getTeamsWithTasks skips missing task states', () => {
    component.status = buildStatus({
      task_ids: ['t1', 'missing'],
      task_states: { t1: { status: 'done', assignee: 'backend', title: 'A' } },
    });
    const result = component.getTeamsWithTasks();
    expect(result.length).toBe(1);
    expect(result[0].tasks.length).toBe(1);
  });

  it('teamLabel returns friendly names', () => {
    expect(component.teamLabel('git_setup')).toBe('Git setup');
    expect(component.teamLabel('devops')).toBe('DevOps');
    expect(component.teamLabel('backend-code-v2')).toBe('Backend (v2)');
    expect(component.teamLabel('frontend-code-v2')).toBe('Frontend (v2)');
    expect(component.teamLabel('backend')).toBe('Backend');
    expect(component.teamLabel('frontend')).toBe('Frontend');
    expect(component.teamLabel('unknown')).toBe('unknown');
  });

  it('taskStatusIcon maps icons', () => {
    expect(component.taskStatusIcon('done')).toBe('check_circle');
    expect(component.taskStatusIcon('failed')).toBe('error');
    expect(component.taskStatusIcon('in_progress')).toBe('pending');
    expect(component.taskStatusIcon('unknown')).toBe('radio_button_unchecked');
  });

  it('taskStatusClass maps classes', () => {
    expect(component.taskStatusClass('done')).toBe('task-done');
    expect(component.taskStatusClass('failed')).toBe('task-failed');
    expect(component.taskStatusClass('in_progress')).toBe('task-active');
    expect(component.taskStatusClass('pending')).toBe('task-pending');
  });

  it('phaseLabel converts snake_case to Title Case', () => {
    expect(component.phaseLabel('product_analysis')).toBe('Product Analysis');
    expect(component.phaseLabel('')).toBe('');
    expect(component.phaseLabel('execution')).toBe('Execution');
  });

  it('isCurrentTask compares with team_progress.current_task_id', () => {
    component.status = buildStatus({
      team_progress: { backend: { current_task_id: 't1' } as never },
    });
    expect(component.isCurrentTask('backend', 't1')).toBe(true);
    expect(component.isCurrentTask('backend', 't2')).toBe(false);
    expect(component.isCurrentTask('frontend', 't1')).toBe(false);
  });

  it('getTeamProgressKeys returns ordered keys with extras at end', () => {
    component.status = buildStatus({
      team_progress: {
        frontend: {} as never,
        custom_team: {} as never,
        backend: {} as never,
      },
    });
    const keys = component.getTeamProgressKeys();
    expect(keys.indexOf('backend')).toBeLessThan(keys.indexOf('frontend'));
    expect(keys.indexOf('custom_team')).toBeGreaterThan(keys.indexOf('frontend'));
  });

  it('getTeamProgressKeys empty when no team_progress', () => {
    component.status = buildStatus();
    expect(component.getTeamProgressKeys()).toEqual([]);
  });

  // -----------------------------------------------------------------------
  // Subprocess helpers
  // -----------------------------------------------------------------------

  it('getPlanningSubprocessPhases / getCodeTeamPhases / getProductAnalysisPhases / getMicrotaskPhases return arrays', () => {
    expect(component.getPlanningSubprocessPhases().length).toBeGreaterThan(0);
    expect(component.getCodeTeamPhases().length).toBeGreaterThan(0);
    expect(component.getProductAnalysisPhases().length).toBeGreaterThan(0);
    expect(component.getMicrotaskPhases().length).toBeGreaterThan(0);
  });

  it('isPlanningSubprocessCompleted/Current/Pending', () => {
    component.status = buildStatus({
      planning_completed_phases: ['intake'],
      planning_subprocess: 'planning',
    });
    expect(component.isPlanningSubprocessCompleted('intake')).toBe(true);
    expect(component.isPlanningSubprocessCompleted('planning')).toBe(false);
    expect(component.isPlanningSubprocessCurrent('planning')).toBe(true);
    expect(component.isPlanningSubprocessPending('review')).toBe(true);
  });

  it('isCodeTeamPhaseCompleted handles missing team_progress', () => {
    component.status = buildStatus();
    expect(component.isCodeTeamPhaseCompleted('backend-code-v2', 'setup')).toBe(false);
  });

  it('isCodeTeamPhaseCompleted / Current / Pending', () => {
    component.status = buildStatus({
      team_progress: { 'backend-code-v2': { current_phase: 'execution' } as never },
    });
    expect(component.isCodeTeamPhaseCompleted('backend-code-v2', 'setup')).toBe(true);
    expect(component.isCodeTeamPhaseCompleted('backend-code-v2', 'planning')).toBe(true);
    expect(component.isCodeTeamPhaseCompleted('backend-code-v2', 'execution')).toBe(false);
    expect(component.isCodeTeamPhaseCurrent('backend-code-v2', 'execution')).toBe(true);
    expect(component.isCodeTeamPhasePending('backend-code-v2', 'deliver')).toBe(true);
  });

  it('isCodeTeamPhaseCompleted false for unknown phase id', () => {
    component.status = buildStatus({
      team_progress: { 'backend-code-v2': { current_phase: 'execution' } as never },
    });
    expect(component.isCodeTeamPhaseCompleted('backend-code-v2', 'nope')).toBe(false);
  });

  it('getExecutionTeams returns empty without team_progress', () => {
    component.status = buildStatus();
    expect(component.getExecutionTeams()).toEqual([]);
  });

  it('getExecutionTeams filters to code v2 teams', () => {
    component.status = buildStatus({
      team_progress: {
        'backend-code-v2': { current_phase: 'execution', progress: 50 } as never,
        backend: { current_phase: 'execution' } as never,
      },
    });
    const t = component.getExecutionTeams();
    expect(t.length).toBe(1);
    expect(t[0].teamId).toBe('backend-code-v2');
  });

  it('isCodingTeamExecution true for task_graph / coding / execution without v2 teams', () => {
    component.status = buildStatus({ phase: 'task_graph' });
    expect(component.isCodingTeamExecution()).toBe(true);
    component.status = buildStatus({ phase: 'coding' });
    expect(component.isCodingTeamExecution()).toBe(true);
    component.status = buildStatus({ phase: 'execution' });
    expect(component.isCodingTeamExecution()).toBe(true);
    component.status = buildStatus({
      phase: 'execution',
      team_progress: { 'backend-code-v2': { current_phase: 'execution' } as never },
    });
    expect(component.isCodingTeamExecution()).toBe(false);
  });

  it('showPlanningSubprocess / showExecutionSubprocess gating', () => {
    component.status = buildStatus({ phase: 'planning', planning_subprocess: 'intake' });
    expect(component.showPlanningSubprocess()).toBe(true);
    component.status = buildStatus({ phase: 'planning' });
    expect(component.showPlanningSubprocess()).toBe(false);

    component.status = buildStatus({
      phase: 'execution',
      team_progress: { 'backend-code-v2': { current_phase: 'execution' } as never },
    });
    expect(component.showExecutionSubprocess()).toBe(true);
    component.status = buildStatus({ phase: 'execution' });
    expect(component.showExecutionSubprocess()).toBe(false);
  });

  it('isAnalysisSubprocessCompleted/Current/Pending with completed phase short-circuits', () => {
    component.status = buildStatus({ phase: 'completed' });
    expect(component.isAnalysisSubprocessCompleted('spec_review')).toBe(true);
    expect(component.isAnalysisSubprocessCurrent('spec_review')).toBe(false);

    component.status = buildStatus({
      phase: 'product_analysis',
      analysis_completed_phases: ['spec_review'],
      analysis_subprocess: 'communicate',
    });
    expect(component.isAnalysisSubprocessCompleted('spec_review')).toBe(true);
    expect(component.isAnalysisSubprocessCurrent('communicate')).toBe(true);
    expect(component.isAnalysisSubprocessPending('spec_update')).toBe(true);
  });

  it('showProductAnalysisSubprocess respects gating', () => {
    component.status = buildStatus({ phase: 'product_analysis', analysis_subprocess: 'spec_review' });
    expect(component.showProductAnalysisSubprocess()).toBe(true);
    component.status = buildStatus({ phase: 'planning' });
    expect(component.showProductAnalysisSubprocess()).toBe(false);
  });

  // -----------------------------------------------------------------------
  // Task title helpers
  // -----------------------------------------------------------------------

  it('getCurrentTaskTitle returns title or fallback', () => {
    component.status = buildStatus({
      current_task: 't1',
      task_states: { t1: { status: 'in_progress', assignee: 'backend', title: 'Make API' } },
    });
    expect(component.getCurrentTaskTitle()).toBe('Make API');
    component.status = buildStatus({ current_task: 'unknown_task' });
    expect(component.getCurrentTaskTitle()).toBe('unknown_task');
    component.status = buildStatus();
    expect(component.getCurrentTaskTitle()).toBe('');
  });

  it('getTaskTitle returns title or fallback', () => {
    component.status = buildStatus({
      team_progress: { backend: { current_task_id: 't1' } as never },
      task_states: { t1: { status: 'in_progress', assignee: 'backend', title: 'Make API' } },
    });
    expect(component.getTaskTitle('backend')).toBe('Make API');
    component.status = buildStatus({
      team_progress: { backend: { current_task_id: 't1' } as never },
    });
    expect(component.getTaskTitle('backend')).toBe('t1');
    component.status = buildStatus({});
    expect(component.getTaskTitle('backend')).toBeNull();
  });

  // -----------------------------------------------------------------------
  // Microtask phases
  // -----------------------------------------------------------------------

  it('isMicrotaskPhaseCompleted/Current/Pending', () => {
    component.status = buildStatus({
      team_progress: {
        backend: { current_microtask_phase: 'qa_testing' } as never,
      },
    });
    expect(component.isMicrotaskPhaseCompleted('backend', 'coding')).toBe(true);
    expect(component.isMicrotaskPhaseCompleted('backend', 'code_review')).toBe(true);
    expect(component.isMicrotaskPhaseCompleted('backend', 'qa_testing')).toBe(false);
    expect(component.isMicrotaskPhaseCurrent('backend', 'qa_testing')).toBe(true);
    expect(component.isMicrotaskPhasePending('backend', 'documentation')).toBe(true);
  });

  it('isMicrotaskPhaseCompleted maps review/problem_solving to code_review', () => {
    component.status = buildStatus({
      team_progress: { backend: { current_microtask_phase: 'review' } as never },
    });
    expect(component.isMicrotaskPhaseCurrent('backend', 'code_review')).toBe(true);
    component.status = buildStatus({
      team_progress: { backend: { current_microtask_phase: 'problem_solving' } as never },
    });
    expect(component.isMicrotaskPhaseCurrent('backend', 'code_review')).toBe(true);
  });

  it('isMicrotaskPhaseCompleted true when phase is completed marker', () => {
    component.status = buildStatus({
      team_progress: { backend: { current_microtask_phase: 'completed' } as never },
    });
    expect(component.isMicrotaskPhaseCompleted('backend', 'coding')).toBe(true);
    expect(component.isMicrotaskPhaseCompleted('backend', 'documentation')).toBe(true);
  });

  it('isMicrotaskPhaseCompleted false when no current phase', () => {
    component.status = buildStatus({ team_progress: { backend: {} as never } });
    expect(component.isMicrotaskPhaseCompleted('backend', 'coding')).toBe(false);
  });

  it('showMicrotaskPhases requires execution phase + current_microtask', () => {
    component.status = buildStatus({
      team_progress: {
        backend: { current_phase: 'execution', current_microtask: 'add tests' } as never,
      },
    });
    expect(component.showMicrotaskPhases('backend')).toBe(true);
    component.status = buildStatus({
      team_progress: { backend: { current_phase: 'planning' } as never },
    });
    expect(component.showMicrotaskPhases('backend')).toBe(false);
  });

  // -----------------------------------------------------------------------
  // Progress tree (flat)
  // -----------------------------------------------------------------------

  it('buildProgressTree returns empty when no status', () => {
    component.status = null;
    expect(component.buildProgressTree()).toEqual([]);
  });

  it('buildProgressTree includes a root + main phases', () => {
    component.status = buildStatus({
      status: 'running',
      repo_path: '/tmp/repo',
      phase: 'execution',
    });
    const nodes = component.buildProgressTree();
    expect(nodes[0].id).toBe('job');
    expect(nodes[0].label).toBe('/tmp/repo');
    expect(nodes[0].status).toBe('current');
    expect(nodes.some((n) => n.id === 'phase-product_analysis')).toBe(true);
    expect(nodes.some((n) => n.id === 'phase-planning')).toBe(true);
    expect(nodes.some((n) => n.id === 'phase-execution')).toBe(true);
    expect(nodes.some((n) => n.id === 'phase-completed')).toBe(true);
  });

  it('buildProgressTree root status maps job statuses', () => {
    component.status = buildStatus({ status: 'completed' });
    expect(component.buildProgressTree()[0].status).toBe('completed');
    component.status = buildStatus({ status: 'failed' });
    expect(component.buildProgressTree()[0].status).toBe('pending');
    component.status = buildStatus({ status: 'pending' });
    expect(component.buildProgressTree()[0].status).toBe('pending');
  });

  it('buildProgressTree falls back to "Job" when no repo_path', () => {
    component.status = buildStatus({});
    const nodes = component.buildProgressTree();
    expect(nodes[0].label).toBe('Job');
  });

  it('buildProgressTree adds analysis subtree with completed/current statuses', () => {
    component.status = buildStatus({
      phase: 'product_analysis',
      analysis_subprocess: 'communicate',
      analysis_completed_phases: ['spec_review'],
    });
    const nodes = component.buildProgressTree();
    const specReview = nodes.find((n) => n.id === 'analysis-spec_review');
    const communicate = nodes.find((n) => n.id === 'analysis-communicate');
    expect(specReview?.status).toBe('completed');
    expect(communicate?.status).toBe('current');
  });

  it('buildProgressTree adds planning subtree', () => {
    component.status = buildStatus({
      phase: 'planning',
      planning_subprocess: 'planning',
      planning_completed_phases: ['intake'],
    });
    const nodes = component.buildProgressTree();
    expect(nodes.find((n) => n.id === 'planning-intake')?.status).toBe('completed');
    expect(nodes.find((n) => n.id === 'planning-planning')?.status).toBe('current');
  });

  it('buildProgressTree adds execution subtree with teams, tasks, microtasks', () => {
    component.status = buildStatus({
      phase: 'execution',
      team_progress: {
        'backend-code-v2': {
          current_phase: 'execution',
          progress: 60,
          current_task_id: 't1',
          current_microtask: 'add tests',
          current_microtask_index: 2,
          microtasks_total: 5,
          current_microtask_phase: 'coding',
        } as never,
      },
      task_states: { t1: { status: 'in_progress', assignee: 'backend', title: 'Make API' } },
    });
    const nodes = component.buildProgressTree();
    expect(nodes.some((n) => n.id === 'team-backend-code-v2')).toBe(true);
    expect(nodes.some((n) => n.id === 'team-backend-code-v2-task')).toBe(true);
    expect(nodes.find((n) => n.id === 'team-backend-code-v2-microtask')?.detail).toBe('(2/5)');
    expect(nodes.some((n) => n.id === 'team-backend-code-v2-microtask-phase-coding')).toBe(true);
  });

  it('buildProgressTree handles execution subtree without microtask index data', () => {
    component.status = buildStatus({
      phase: 'execution',
      team_progress: {
        'backend-code-v2': {
          current_phase: 'execution',
          current_task_id: 't1',
          current_microtask: 'add tests',
        } as never,
      },
      task_states: { t1: { status: 'in_progress', assignee: 'backend', title: 'Make API' } },
    });
    const nodes = component.buildProgressTree();
    const micro = nodes.find((n) => n.id === 'team-backend-code-v2-microtask');
    expect(micro?.detail).toBeUndefined();
  });

  it('buildProgressTree: team with no current task skips task node', () => {
    component.status = buildStatus({
      phase: 'execution',
      team_progress: {
        'backend-code-v2': { current_phase: 'setup' } as never,
      },
    });
    const nodes = component.buildProgressTree();
    expect(nodes.some((n) => n.id === 'team-backend-code-v2')).toBe(true);
    expect(nodes.some((n) => n.id === 'team-backend-code-v2-task')).toBe(false);
  });

  it('getTreeConnectorClass returns expected class names', () => {
    expect(component.getTreeConnectorClass({ level: 0, isLast: true } as never)).toBe('');
    expect(component.getTreeConnectorClass({ level: 1, isLast: true } as never)).toBe('tree-connector-last');
    expect(component.getTreeConnectorClass({ level: 1, isLast: false } as never)).toBe('tree-connector-mid');
  });

  // -----------------------------------------------------------------------
  // DAG tree
  // -----------------------------------------------------------------------

  it('buildDAGTree returns empty array when status is null', () => {
    component.status = null;
    expect(component.buildDAGTree()).toEqual([]);
  });

  it('buildDAGTree returns all main phases', () => {
    component.status = buildStatus({ phase: 'planning' });
    const tree = component.buildDAGTree();
    expect(tree.length).toBe(4);
    expect(tree.map((n) => n.id)).toEqual([
      'phase-product_analysis',
      'phase-planning',
      'phase-execution',
      'phase-completed',
    ]);
  });

  it('buildDAGTree children of product_analysis use analysis subprocess data', () => {
    component.status = buildStatus({
      phase: 'product_analysis',
      analysis_subprocess: 'communicate',
      analysis_completed_phases: ['spec_review'],
    });
    const tree = component.buildDAGTree();
    const pa = tree.find((n) => n.id === 'phase-product_analysis');
    expect(pa?.children?.some((c) => c.id === 'analysis-spec_review' && c.status === 'completed')).toBe(true);
    expect(pa?.children?.some((c) => c.id === 'analysis-communicate' && c.status === 'current')).toBe(true);
  });

  it('buildDAGTree children of planning use planning subprocess data', () => {
    component.status = buildStatus({
      phase: 'planning',
      planning_subprocess: 'planning',
      planning_completed_phases: ['intake'],
    });
    const tree = component.buildDAGTree();
    const plan = tree.find((n) => n.id === 'phase-planning');
    expect(plan?.children?.some((c) => c.id === 'planning-intake' && c.status === 'completed')).toBe(true);
    expect(plan?.children?.some((c) => c.id === 'planning-planning' && c.status === 'current')).toBe(true);
  });

  it('buildDAGTree children of execution include teams + their phases', () => {
    component.status = buildStatus({
      phase: 'execution',
      team_progress: {
        'backend-code-v2': {
          current_phase: 'execution',
          progress: 80,
          current_microtask: 'tests',
          current_task_id: 't1',
        } as never,
      },
      task_states: { t1: { status: 'in_progress', assignee: 'backend', title: 'X' } },
    });
    const tree = component.buildDAGTree();
    const exec = tree.find((n) => n.id === 'phase-execution');
    const team = exec?.children?.[0];
    expect(team?.label).toBe('Backend (v2)');
    expect(team?.detail).toBe('80%');
    const execPhase = team?.children?.find((c) => c.id === 'team-backend-code-v2-phase-execution');
    expect(execPhase?.children?.length).toBeGreaterThan(0);
  });

  it('buildDAGTree team detail omitted when progress is null', () => {
    component.status = buildStatus({
      phase: 'execution',
      team_progress: {
        'backend-code-v2': { current_phase: 'planning' } as never,
      },
    });
    const tree = component.buildDAGTree();
    const exec = tree.find((n) => n.id === 'phase-execution');
    expect(exec?.children?.[0]?.detail).toBeUndefined();
  });

  // -----------------------------------------------------------------------
  // getTeamStatus
  // -----------------------------------------------------------------------

  it('getTeamStatus returns pending if no progress', () => {
    component.status = buildStatus();
    const tree = component.buildDAGTree();
    const exec = tree.find((n) => n.id === 'phase-execution');
    expect(exec?.children?.length).toBe(0);
  });

  it('getTeamStatus completed when deliver + 100%', () => {
    component.status = buildStatus({
      phase: 'execution',
      team_progress: {
        'backend-code-v2': { current_phase: 'deliver', progress: 100 } as never,
      },
    });
    const tree = component.buildDAGTree();
    const exec = tree.find((n) => n.id === 'phase-execution');
    expect(exec?.children?.[0]?.status).toBe('completed');
  });

  it('getTeamStatus current when current_phase set but not deliver/100', () => {
    component.status = buildStatus({
      phase: 'execution',
      team_progress: {
        'backend-code-v2': { current_phase: 'setup', progress: 10 } as never,
      },
    });
    const tree = component.buildDAGTree();
    const exec = tree.find((n) => n.id === 'phase-execution');
    expect(exec?.children?.[0]?.status).toBe('current');
  });

  it('getTeamStatus pending when no current_phase', () => {
    component.status = buildStatus({
      phase: 'execution',
      team_progress: { 'backend-code-v2': {} as never },
    });
    const tree = component.buildDAGTree();
    const exec = tree.find((n) => n.id === 'phase-execution');
    expect(exec?.children?.[0]?.status).toBe('pending');
  });

  // -----------------------------------------------------------------------
  // Work tree (additional legacy / hierarchy cases)
  // -----------------------------------------------------------------------

  it('buildWorkTreeRows returns just root row when no tasks', () => {
    const status = buildStatus({ status: 'pending' });
    const rows = (component as never as { buildWorkTreeRows: (s: JobStatusResponse) => unknown[] }).buildWorkTreeRows(status);
    expect(rows.length).toBe(1);
  });

  it('buildWorkTreeRows: legacy fallback creates parents for orphan task', () => {
    const status = buildStatus({
      task_ids: ['t1'],
      task_states: { t1: { status: 'in_progress', assignee: 'backend', title: 'Plain Task' } },
    });
    const rows = (component as never as {
      buildWorkTreeRows: (s: JobStatusResponse) => { label: string; level: string }[];
    }).buildWorkTreeRows(status);
    expect(rows.some((r) => r.label === 'Uncategorized Initiative')).toBe(true);
    expect(rows.some((r) => r.label === 'General Epic')).toBe(true);
    expect(rows.some((r) => r.label === 'Plain Task')).toBe(true);
  });

  it('buildWorkTreeRows: legacy fallback handles subtask with no task parent', () => {
    const status = buildStatus({
      task_ids: ['st1'],
      task_states: { st1: { status: 'pending', assignee: 'backend', title: 'Subtask: build widget' } },
    });
    const rows = (component as never as {
      buildWorkTreeRows: (s: JobStatusResponse) => { label: string; level: string }[];
    }).buildWorkTreeRows(status);
    expect(rows.some((r) => r.label === 'General Task Group')).toBe(true);
  });

  it('buildWorkTreeRows: legacy fallback wires up subtask via dependencies', () => {
    const status = buildStatus({
      task_ids: ['ep1', 'tk1', 'st1'],
      task_states: {
        ep1: { status: 'in_progress', assignee: 'planner', title: 'Epic: Check' },
        tk1: { status: 'in_progress', assignee: 'backend', title: 'Task: do X', dependencies: ['ep1'] },
        st1: { status: 'pending', assignee: 'backend', title: 'Subtask: do Y', dependencies: ['tk1'] },
      },
    });
    const rows = (component as never as {
      buildWorkTreeRows: (s: JobStatusResponse) => { label: string; level: string; depth: number }[];
    }).buildWorkTreeRows(status);
    expect(rows.some((r) => r.label === 'Subtask: do Y' && r.level === 'subtask')).toBe(true);
  });

  it('buildWorkTreeRows: hierarchy attaches task directly to initiative when no epic/story match', () => {
    const status = buildStatus({
      task_ids: ['t1'],
      task_states: { t1: { status: 'pending', assignee: 'backend', title: 'Direct task', initiative_id: 'init-1' } },
      planning_hierarchy: {
        initiatives: [{ id: 'init-1', title: 'My Initiative', description: '' }],
        epics: [],
        stories: [],
      },
    });
    const rows = (component as never as {
      buildWorkTreeRows: (s: JobStatusResponse) => { label: string; level: string }[];
    }).buildWorkTreeRows(status);
    expect(rows.some((r) => r.label === 'My Initiative')).toBe(true);
    expect(rows.some((r) => r.label === 'Direct task')).toBe(true);
  });

  it('buildWorkTreeRows: hierarchy attaches task to epic when story missing', () => {
    const status = buildStatus({
      task_ids: ['t1'],
      task_states: {
        t1: { status: 'pending', assignee: 'backend', title: 'Epic task', initiative_id: 'init-1', epic_id: 'epic-1' },
      },
      planning_hierarchy: {
        initiatives: [{ id: 'init-1', title: 'Init', description: '' }],
        epics: [{ id: 'epic-1', title: 'Ep', description: '', initiative_id: 'init-1' }],
        stories: [],
      },
    });
    const rows = (component as never as {
      buildWorkTreeRows: (s: JobStatusResponse) => { label: string }[];
    }).buildWorkTreeRows(status);
    expect(rows.some((r) => r.label === 'Epic task')).toBe(true);
  });

  it('buildWorkTreeRows: status derivation propagates failed/in_progress upward', () => {
    const status = buildStatus({
      task_ids: ['t1', 't2'],
      task_states: {
        t1: { status: 'failed', assignee: 'backend', title: 'Bad task', initiative_id: 'i1', epic_id: 'e1', story_id: 's1' },
        t2: { status: 'pending', assignee: 'backend', title: 'OK task', initiative_id: 'i1', epic_id: 'e1', story_id: 's1' },
      },
      planning_hierarchy: {
        initiatives: [{ id: 'i1', title: 'I1', description: '' }],
        epics: [{ id: 'e1', title: 'E1', description: '', initiative_id: 'i1' }],
        stories: [{ id: 's1', title: 'S1', description: '', epic_id: 'e1', initiative_id: 'i1' }],
      },
    });
    const rows = (component as never as {
      buildWorkTreeRows: (s: JobStatusResponse) => { label: string; status: string }[];
    }).buildWorkTreeRows(status);
    expect(rows.find((r) => r.label === 'I1')?.status).toBe('failed');
  });

  it('buildWorkTreeRows: status derivation pending when children mixed (no completed) - covers final fallback', () => {
    const status = buildStatus({
      task_ids: ['t1'],
      task_states: {
        t1: { status: 'pending', assignee: 'backend', title: 'T1', story_id: 's1', epic_id: 'e1', initiative_id: 'i1' },
      },
      planning_hierarchy: {
        initiatives: [{ id: 'i1', title: 'I1', description: '' }],
        epics: [{ id: 'e1', title: 'E1', description: '', initiative_id: 'i1' }],
        stories: [{ id: 's1', title: 'S1', description: '', epic_id: 'e1', initiative_id: 'i1' }],
      },
    });
    const rows = (component as never as {
      buildWorkTreeRows: (s: JobStatusResponse) => { label: string; status: string }[];
    }).buildWorkTreeRows(status);
    const i1 = rows.find((r) => r.label === 'I1');
    expect(i1?.status).toBe('pending');
  });

  it('buildWorkTreeRows: root status maps job statuses', () => {
    let status: JobStatusResponse;
    status = buildStatus({ status: 'running', task_ids: ['t1'], task_states: { t1: { status: 'in_progress', assignee: 'backend', title: 't' } } });
    let rows = (component as never as {
      buildWorkTreeRows: (s: JobStatusResponse) => { status: string; depth: number }[];
    }).buildWorkTreeRows(status);
    expect(rows[0].status).toBe('in_progress');
    status = buildStatus({ status: 'failed' });
    rows = (component as never as {
      buildWorkTreeRows: (s: JobStatusResponse) => { status: string; depth: number }[];
    }).buildWorkTreeRows(status);
    expect(rows[0].status).toBe('failed');
    status = buildStatus({ status: 'cancelled' });
    rows = (component as never as {
      buildWorkTreeRows: (s: JobStatusResponse) => { status: string; depth: number }[];
    }).buildWorkTreeRows(status);
    expect(rows[0].status).toBe('failed');
  });

  it('workItemStatusIcon maps icons', () => {
    expect(component.workItemStatusIcon('completed')).toBe('check_circle');
    expect(component.workItemStatusIcon('in_progress')).toBe('autorenew');
    expect(component.workItemStatusIcon('failed')).toBe('error');
    expect(component.workItemStatusIcon('pending')).toBe('radio_button_unchecked');
  });
});

describe('RunTeamTrackingComponent sub-agent activity and staleness', () => {
  let api: { getJobStatus: ReturnType<typeof vi.fn> };
  let fixture: ComponentFixture<RunTeamTrackingComponent>;
  let component: RunTeamTrackingComponent;

  const activityStatus = (overrides: Partial<JobStatusResponse> = {}): JobStatusResponse => ({
    job_id: 'job-1',
    status: 'running',
    phase: 'coding',
    task_results: [],
    task_ids: [],
    failed_tasks: [],
    status_text: 'Code review (40%): Add login — chunk 2/5',
    current_activity: {
      agent: 'code_review',
      step: 'reviewing',
      detail: 'chunk 2/5: src/auth.py',
      fraction: 0.4,
      task_id: 't1',
      task_title: 'Add login',
    },
    ...overrides,
  });

  beforeEach(() => {
    api = { getJobStatus: vi.fn() };
    TestBed.configureTestingModule({
      providers: [{ provide: SoftwareEngineeringApiService, useValue: api }],
    });
    fixture = TestBed.createComponent(RunTeamTrackingComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('activityFraction returns the clamped sub-agent fraction', () => {
    component.status = activityStatus();
    expect(component.activityFraction()).toBe(0.4);

    component.status = activityStatus({
      current_activity: { agent: 'code_review', fraction: 1.7 },
    });
    expect(component.activityFraction()).toBe(1);

    component.status = activityStatus({
      current_activity: { agent: 'code_review', fraction: -0.2 },
    });
    expect(component.activityFraction()).toBe(0);

    component.status = activityStatus({ current_activity: undefined });
    expect(component.activityFraction()).toBeNull();
  });

  it('activityAgentLabel names the reporting sub-agent', () => {
    component.status = activityStatus();
    expect(component.activityAgentLabel()).toBe('Code review');
    component.status = activityStatus({
      current_activity: { agent: 'tech_lead_review', fraction: 0.1 },
    });
    expect(component.activityAgentLabel()).toBe('Tech Lead review');
  });

  /** Render with a directly-assigned status (the poll timer never fires in sync tests). */
  const render = (status: JobStatusResponse): HTMLElement => {
    api.getJobStatus.mockReturnValue(of(status));
    component.jobId = 'job-1';
    component.status = status;
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  };

  it('renders the determinate sub-progress bar and detail in the coding-team card', () => {
    const el = render(activityStatus());
    const detail = el.querySelector('.current-activity-detail');
    expect(detail?.textContent).toContain('Code review: chunk 2/5: src/auth.py');
    const pct = el.querySelector('.current-activity-pct');
    expect(pct?.textContent).toContain('40%');
    const bar = el.querySelector('.current-activity-progress mat-progress-bar');
    expect(bar).toBeTruthy();
  });

  it('shows last-activity label and stalled warning for a stale running job', () => {
    const stale = new Date(Date.now() - 12 * 60 * 1000).toISOString();
    const el = render(activityStatus({ last_activity_at: stale }));
    expect(el.querySelector('.last-activity-section')?.textContent).toContain('Last activity: 12m ago');
    expect(el.querySelector('.stalled-warning')?.textContent).toContain(
      'No agent activity for 12m — the job may be stalled.',
    );
  });

  it('hides the stalled warning when activity is fresh or job is waiting', () => {
    render(activityStatus({ last_activity_at: new Date().toISOString() }));
    expect(fixture.nativeElement.querySelector('.stalled-warning')).toBeNull();

    const stale = new Date(Date.now() - 12 * 60 * 1000).toISOString();
    component.status = activityStatus({ last_activity_at: stale, waiting_for_answers: true });
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.stalled-warning')).toBeNull();
  });

  it('hides the activity card on non-running jobs (a dead run must not render a live sub-bar)', () => {
    // A killed/interrupted orchestrator never runs its finally clears, so a stale
    // current_activity can survive on the record; the card must not present it
    // as live progress.
    const el = render(activityStatus({ status: 'interrupted' }));
    expect(el.querySelector('.current-activity-section')).toBeNull();
  });
});
