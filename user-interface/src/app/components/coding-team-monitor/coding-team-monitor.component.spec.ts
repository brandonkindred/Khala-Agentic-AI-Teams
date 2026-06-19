import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { CodingTeamMonitorComponent } from './coding-team-monitor.component';
import type { CodingTeamAgentStatus, CodingTeamJobStatus } from '../../models/coding-team.model';

describe('CodingTeamMonitorComponent', () => {
  let component: CodingTeamMonitorComponent;
  let fixture: ComponentFixture<CodingTeamMonitorComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [CodingTeamMonitorComponent, NoopAnimationsModule],
    }).compileComponents();
    fixture = TestBed.createComponent(CodingTeamMonitorComponent);
    component = fixture.componentInstance;
  });

  /** Set the @Input status, run change detection, and return the rendered host element. Uses
   *  setInput so an OnPush component re-renders when the status changes between renders. */
  async function render(status: Partial<CodingTeamJobStatus> | null): Promise<HTMLElement> {
    fixture.componentRef.setInput('status', status as CodingTeamJobStatus | null);
    fixture.detectChanges();
    await fixture.whenStable();
    return fixture.nativeElement as HTMLElement;
  }

  function agent(over: Partial<CodingTeamAgentStatus>): CodingTeamAgentStatus {
    return {
      agent_id: 'a',
      role: 'senior_engineer',
      display_name: 'A',
      stack: null,
      tools_services: [],
      status: 'idle',
      current_task_id: null,
      current_task_title: null,
      current_step: null,
      activity_detail: null,
      activity_fraction: null,
      ...over,
    };
  }

  it('renders nothing when status is null', async () => {
    const el = await render(null);
    expect(el.querySelector('.ct-monitor')).toBeNull();
  });

  it('renders the monitor container once a status is present', async () => {
    const el = await render({ job_id: 'j1', status: 'running' });
    expect(el.querySelector('.ct-monitor')).not.toBeNull();
  });

  // --- Objective ---------------------------------------------------------------

  it('shows status_text as the objective when present', async () => {
    const el = await render({ job_id: 'j1', status: 'running', status_text: 'Implementing: Build UI' });
    expect(el.querySelector('.ct-monitor__objective-detail')?.textContent).toContain(
      'Implementing: Build UI',
    );
  });

  it('falls back to a phase-derived objective when status_text is absent', () => {
    const c = component;
    c.status = { job_id: 'j', status: 'running', phase: 'task_graph' } as CodingTeamJobStatus;
    expect(c.objectiveText()).toBe('Building the task graph');
    c.status = { ...c.status, phase: 'coding' };
    expect(c.objectiveText()).toBe('Implementing the task graph');
    c.status = { ...c.status, phase: 'publishing' };
    expect(c.objectiveText()).toBe('Opening the pull request');
    c.status = { ...c.status, phase: 'reviewing' };
    expect(c.objectiveText()).toBe('Reviewing the pull request');
    c.status = { ...c.status, phase: 'paused' };
    expect(c.objectiveText()).toBe('Paused — waiting for input');
    c.status = { ...c.status, phase: 'completed' };
    expect(c.objectiveText()).toBe('Run complete');
    c.status = { ...c.status, phase: 'mystery' };
    expect(c.objectiveText()).toBe('Coding team run');
    c.status = null;
    expect(c.objectiveText()).toBe('Coding team run');
  });

  it('lists the titles of in-progress and in-review tasks as the current focus', async () => {
    const el = await render({
      job_id: 'j1',
      status: 'running',
      task_graph_snapshot: [
        { id: 't1', title: 'Build UI', status: 'in_progress' },
        { id: 't2', title: 'API', status: 'in_review' },
        { id: 't3', title: 'Done thing', status: 'merged' },
        { id: 't4', title: '', status: 'in_progress' },
      ],
    });
    const tasks = el.querySelector('.ct-monitor__objective-tasks')?.textContent ?? '';
    expect(tasks).toContain('Build UI');
    expect(tasks).toContain('API');
    expect(tasks).not.toContain('Done thing');
  });

  // --- Overall progress + stepper ----------------------------------------------

  it('renders a determinate progress bar with the percent when progress is known', async () => {
    const el = await render({ job_id: 'j1', status: 'running', progress: 47 });
    expect(component.overallProgress()).toBe(47);
    expect(component.progressMode()).toBe('determinate');
    expect(el.querySelector('.ct-monitor__progress-pct')?.textContent).toContain('47%');
    expect(el.querySelector('mat-progress-bar')).not.toBeNull();
  });

  it('clamps out-of-range progress and hides the percent when absent', async () => {
    await render({ job_id: 'j1', status: 'running', progress: 250 });
    expect(component.overallProgress()).toBe(100);
    const el = await render({ job_id: 'j1', status: 'running' });
    expect(component.overallProgress()).toBeNull();
    expect(el.querySelector('.ct-monitor__progress-pct')).toBeNull();
  });

  it('is indeterminate while a started job has no progress yet, determinate otherwise', () => {
    const c = component;
    for (const status of ['running', 'pending']) {
      c.status = { job_id: 'j', status } as CodingTeamJobStatus;
      expect(c.progressMode()).toBe('indeterminate');
    }
    c.status = { job_id: 'j', status: 'waiting_for_user' } as CodingTeamJobStatus;
    expect(c.progressMode()).toBe('determinate');
    c.status = { job_id: 'j', status: 'running', progress: 10 } as CodingTeamJobStatus;
    expect(c.progressMode()).toBe('determinate');
  });

  it('uses the warn color when the job failed, was cancelled, or completed with failures', () => {
    const c = component;
    for (const status of ['failed', 'cancelled', 'completed_with_failures']) {
      c.status = { job_id: 'j', status } as CodingTeamJobStatus;
      expect(c.progressColor()).toBe('warn');
    }
    for (const status of ['running', 'completed', 'pending']) {
      c.status = { job_id: 'j', status } as CodingTeamJobStatus;
      expect(c.progressColor()).toBe('primary');
    }
  });

  it('marks stepper phases completed/current/pending from the phase', () => {
    const c = component;
    c.status = { job_id: 'j', status: 'running', phase: 'coding' } as CodingTeamJobStatus;
    expect(c.isPhaseCompleted('task_graph')).toBe(true);
    expect(c.isCurrentPhase('coding')).toBe(true);
    expect(c.isFailedPhase('coding')).toBe(false);
    expect(c.isPhasePending('completed')).toBe(true);
  });

  it('treats a terminal success as fully completed in the stepper', () => {
    const c = component;
    for (const status of ['completed', 'completed_with_failures', 'already_complete']) {
      c.status = { job_id: 'j', status } as CodingTeamJobStatus;
      expect(c.isPhaseCompleted('task_graph')).toBe(true);
      expect(c.isPhaseCompleted('coding')).toBe(true);
      expect(c.isPhaseCompleted('completed')).toBe(true);
      // A finished step is NOT also "current" — that would put two conflicting classes on it.
      expect(c.isCurrentPhase('completed')).toBe(false);
      expect(c.isFailedPhase('completed')).toBe(false);
    }
  });

  it('does NOT render a failed run as all-completed; the reached step is failed, not green', () => {
    // The orchestrator stamps phase='completed' on failure — the stepper must not read as success.
    const c = component;
    c.status = {
      job_id: 'j',
      status: 'failed',
      phase: 'completed',
      task_graph_snapshot: [{ id: 't1', title: 'x', status: 'in_progress' }],
    } as CodingTeamJobStatus;
    expect(c.isPhaseCompleted('task_graph')).toBe(true); // planning got done
    expect(c.isFailedPhase('coding')).toBe(true); // stopped (failed) in coding
    expect(c.isCurrentPhase('coding')).toBe(false); // failed, not "in progress"
    expect(c.isPhaseCompleted('completed')).toBe(false); // NOT green
    expect(c.isPhasePending('completed')).toBe(true);
  });

  it('locates a failure during planning (no task graph) on the Planning step', () => {
    const c = component;
    c.status = { job_id: 'j', status: 'failed', phase: 'completed' } as CodingTeamJobStatus;
    expect(c.isFailedPhase('task_graph')).toBe(true);
    expect(c.isPhaseCompleted('coding')).toBe(false);
  });

  it('keeps the stepper on Coding (not blank) during the publishing and reviewing phases', () => {
    // _defer_terminal_success rewrites a finished GitHub-issue run to (running, publishing) while
    // it pushes the branch and opens the PR; the /review-pr flow uses phase='reviewing'.
    const c = component;
    for (const phase of ['publishing', 'reviewing']) {
      c.status = { job_id: 'j', status: 'running', phase } as CodingTeamJobStatus;
      expect(c.isPhaseCompleted('task_graph')).toBe(true);
      expect(c.isCurrentPhase('coding')).toBe(true);
      expect(c.isPhasePending('completed')).toBe(true);
    }
  });

  it('maps a live task_graph phase to Planning and a live completed phase to the Completed step', () => {
    const c = component;
    c.status = { job_id: 'j', status: 'running', phase: 'task_graph' } as CodingTeamJobStatus;
    expect(c.isCurrentPhase('task_graph')).toBe(true);
    // A non-terminal status that nonetheless reports phase='completed' (e.g. a transient state)
    // still resolves the Completed step rather than falling through.
    c.status = { job_id: 'j', status: 'running', phase: 'completed' } as CodingTeamJobStatus;
    expect(c.isCurrentPhase('completed')).toBe(true);
    expect(c.isPhaseCompleted('coding')).toBe(true);
  });

  it('infers the paused step from whether a task graph exists yet', () => {
    const c = component;
    c.status = { job_id: 'j', status: 'waiting_for_user', phase: 'paused' } as CodingTeamJobStatus;
    expect(c.isCurrentPhase('task_graph')).toBe(true);
    c.status = {
      job_id: 'j',
      status: 'waiting_for_user',
      phase: 'paused',
      task_graph_snapshot: [{ id: 't1', title: 'x', status: 'to_do' }],
    } as CodingTeamJobStatus;
    expect(c.isCurrentPhase('coding')).toBe(true);
  });

  it('defaults an unknown phase on a live run to Planning, and pins nothing for a null status', () => {
    const c = component;
    c.status = { job_id: 'j', status: 'running', phase: 'mystery' } as CodingTeamJobStatus;
    expect(c.isCurrentPhase('task_graph')).toBe(true); // not blank
    expect(c.isPhasePending('coding')).toBe(true);
    c.status = null;
    expect(c.isCurrentPhase('task_graph')).toBe(false);
    expect(c.isPhasePending('task_graph')).toBe(true);
  });

  // --- Stepper DOM (rendering, not just method returns) ------------------------

  it('renders a red failed step in the DOM for a failed run (not an all-green stepper)', async () => {
    const el = await render({
      job_id: 'j',
      status: 'failed',
      phase: 'completed',
      task_graph_snapshot: [{ id: 't1', title: 'x', status: 'in_progress' }],
    });
    expect(el.querySelector('.ct-step.failed')).not.toBeNull(); // the reached (Coding) step is red
    expect(el.querySelector('.ct-step.completed')).not.toBeNull(); // Planning got done
    // Three steps, and the last (Completed) is neither completed nor failed — i.e. not green.
    const steps = el.querySelectorAll('.ct-step');
    expect(steps[2].classList.contains('completed')).toBe(false);
    expect(steps[2].classList.contains('failed')).toBe(false);
  });

  it('keeps a current step in the DOM (never blank) during the publishing phase', async () => {
    const el = await render({ job_id: 'j', status: 'running', phase: 'publishing' });
    expect(el.querySelector('.ct-step.current')).not.toBeNull();
  });

  it('does not also mark the completed step current on a finished run (no class overlap)', async () => {
    const el = await render({ job_id: 'j', status: 'completed' });
    const completedStep = el.querySelectorAll('.ct-step')[2];
    expect(completedStep.classList.contains('completed')).toBe(true);
    expect(completedStep.classList.contains('current')).toBe(false);
  });

  // --- Live sub-agent activity -------------------------------------------------

  it('renders the current-activity sub-bar with label, detail, and a clamped fraction', async () => {
    const el = await render({
      job_id: 'j1',
      status: 'running',
      current_activity: { agent: 'code_review', detail: 'src/app.py', fraction: 1.5 },
    });
    expect(el.querySelector('.ct-monitor__activity-label')?.textContent).toContain('Code review');
    expect(el.querySelector('.ct-monitor__activity-detail')?.textContent).toContain('src/app.py');
    expect(component.activityFraction()).toBe(1);
    expect(el.querySelector('.ct-monitor__activity-bar')).not.toBeNull();
  });

  it('labels the activity agent and falls back gracefully', () => {
    const c = component;
    c.status = { current_activity: { agent: 'tech_lead_review' } } as CodingTeamJobStatus;
    expect(c.activityAgentLabel()).toBe('Tech Lead review');
    c.status = { current_activity: { agent: 'something_else' } } as CodingTeamJobStatus;
    expect(c.activityAgentLabel()).toBe('something_else');
    c.status = { current_activity: {} } as CodingTeamJobStatus;
    expect(c.activityAgentLabel()).toBe('Agent activity');
  });

  it('hides the activity fraction bar when no fraction is reported', async () => {
    const el = await render({
      job_id: 'j1',
      status: 'running',
      current_activity: { agent: 'code_review', detail: 'parsing' },
    });
    expect(component.activityFraction()).toBeNull();
    expect(el.querySelector('.ct-monitor__activity-bar')).toBeNull();
  });

  // --- Agent roster ------------------------------------------------------------

  it('renders an agent card per roster entry with status badge and active emphasis', async () => {
    const el = await render({
      job_id: 'j1',
      status: 'running',
      agents: [
        agent({ agent_id: 'tech_lead', role: 'tech_lead', display_name: 'Tech Lead', status: 'reviewing' }),
        agent({
          agent_id: 'frontend',
          display_name: 'Senior Engineer — frontend',
          status: 'working',
          stack: 'frontend',
          tools_services: ['Angular'],
          current_task_title: 'Build UI',
          current_step: 'reviewing',
          activity_detail: 'chunk 1/2',
          activity_fraction: 0.5,
        }),
        agent({ agent_id: 'backend', display_name: 'Senior Engineer — backend', status: 'idle' }),
      ],
    });
    const cards = el.querySelectorAll('.ct-agent');
    expect(cards.length).toBe(3);
    expect(el.querySelector('.ct-agent__status--reviewing')?.textContent).toContain('Reviewing');
    expect(el.querySelector('.ct-agent--working')).not.toBeNull();
    // active emphasis on non-idle agents only
    expect(el.querySelectorAll('.ct-agent--active').length).toBe(2);
    // the working engineer surfaces its task, tools, and a per-agent activity bar
    expect(el.textContent).toContain('Build UI');
    expect(el.querySelector('.ct-agent__tool')?.textContent).toContain('Angular');
    expect(el.querySelector('.ct-agent__activity mat-progress-bar')).not.toBeNull();
  });

  it('maps known agent statuses to a safe class suffix and folds anything else to unknown', () => {
    const c = component;
    for (const status of ['working', 'in_review', 'reviewing', 'planning', 'idle']) {
      expect(c.agentStatusClass(agent({ status }))).toBe(status);
    }
    expect(c.agentStatusClass(agent({ status: 'merged' }))).toBe('unknown');
    expect(c.agentStatusClass(agent({ status: 'weird value with spaces' }))).toBe('unknown');
  });

  it('exposes role icons, status labels, active flag, and clamped per-agent fraction', () => {
    const c = component;
    expect(c.agentRoleIcon(agent({ role: 'tech_lead' }))).toBe('supervisor_account');
    expect(c.agentRoleIcon(agent({ role: 'senior_engineer' }))).toBe('code');
    expect(c.agentStatusLabel(agent({ status: 'working' }))).toBe('Working');
    expect(c.agentStatusLabel(agent({ status: 'in_review' }))).toBe('In review');
    expect(c.agentStatusLabel(agent({ status: 'reviewing' }))).toBe('Reviewing');
    expect(c.agentStatusLabel(agent({ status: 'planning' }))).toBe('Planning');
    expect(c.agentStatusLabel(agent({ status: 'idle' }))).toBe('Idle');
    expect(c.agentStatusLabel(agent({ status: 'weird' }))).toBe('weird');
    expect(c.isAgentActive(agent({ status: 'idle' }))).toBe(false);
    expect(c.isAgentActive(agent({ status: 'working' }))).toBe(true);
    expect(c.agentFraction(agent({ activity_fraction: 2 }))).toBe(1);
    expect(c.agentFraction(agent({ activity_fraction: -1 }))).toBe(0);
    expect(c.agentFraction(agent({}))).toBeNull();
  });

  it('omits the roster section entirely when there are no agents', async () => {
    const el = await render({ job_id: 'j1', status: 'running', agents: [] });
    expect(el.querySelector('.ct-monitor__agents')).toBeNull();
  });

  it('renders distinct cards for two same-stack engineers (backend disambiguates the ids)', async () => {
    // Same-named stacks get unique agent_ids from the backend (backend / backend_2), so tracking
    // by agent.agent_id renders both without an NG0955 duplicate-key error.
    const el = await render({
      job_id: 'j1',
      status: 'running',
      agents: [
        agent({ agent_id: 'backend', display_name: 'Senior Engineer — backend', status: 'working' }),
        agent({ agent_id: 'backend_2', display_name: 'Senior Engineer — backend', status: 'idle' }),
      ],
    });
    expect(el.querySelectorAll('.ct-agent').length).toBe(2);
  });

  it('exposes the monitor as a polite live region for assistive technology', async () => {
    const monitor = (await render({ job_id: 'j1', status: 'running' })).querySelector('.ct-monitor');
    expect(monitor?.getAttribute('role')).toBe('status');
    expect(monitor?.getAttribute('aria-live')).toBe('polite');
  });
});
