import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { CodingTeamMonitorComponent } from './coding-team-monitor.component';
import type { CodingTeamAgentStatus, CodingTeamJobStatus } from '../../models/coding-team.model';
import { expectNoAxeViolations } from '../../testing/a11y';

describe('CodingTeamMonitorComponent a11y', () => {
  let fixture: ComponentFixture<CodingTeamMonitorComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [CodingTeamMonitorComponent, NoopAnimationsModule],
    }).compileComponents();
    fixture = TestBed.createComponent(CodingTeamMonitorComponent);
  });

  /** Set the @Input status, run change detection, and return the rendered host element. */
  async function render(status: Partial<CodingTeamJobStatus> | null): Promise<HTMLElement> {
    fixture.componentRef.setInput('status', status as CodingTeamJobStatus | null);
    fixture.detectChanges();
    await fixture.whenStable();
    return fixture.nativeElement as HTMLElement;
  }

  function agent(over: Partial<CodingTeamAgentStatus>): CodingTeamAgentStatus {
    return {
      agent_id: 'a',
      role: 'implementation_worker',
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

  it('has no axe violations for a running job with a determinate progress bar', async () => {
    const el = await render({ job_id: 'j1', status: 'running', phase: 'coding', progress: 47 });
    expect(el.querySelector('.ct-monitor')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('exposes exactly one polite live region, carrying the summary text', async () => {
    const el = await render({ job_id: 'j1', status: 'running', phase: 'coding', progress: 47 });
    const liveRegions = el.querySelectorAll('[aria-live]');
    expect(liveRegions.length).toBe(1);
    expect(liveRegions[0].getAttribute('aria-live')).toBe('polite');
    expect(liveRegions[0].textContent).toContain('47% complete');
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations for a pending job with an indeterminate progress bar', async () => {
    const el = await render({ job_id: 'j1', status: 'pending' });
    expect(el.querySelector('.ct-monitor')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations for a completed job with a fully-completed stepper', async () => {
    const el = await render({ job_id: 'j1', status: 'completed' });
    expect(el.querySelectorAll('.ct-step.completed').length).toBeGreaterThan(0);
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations for a failed job with a failed stepper step', async () => {
    const el = await render({
      job_id: 'j1',
      status: 'failed',
      phase: 'completed',
      task_graph_snapshot: [{ id: 't1', title: 'x', status: 'in_progress' }],
    });
    expect(el.querySelector('.ct-step.failed')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations for a job waiting on the user', async () => {
    const el = await render({ job_id: 'j1', status: 'waiting_for_user', phase: 'paused' });
    expect(el.querySelector('.ct-monitor')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations with the agent roster and current-activity sub-bar rendered', async () => {
    const el = await render({
      job_id: 'j1',
      status: 'running',
      current_activity: { agent: 'code_review', detail: 'src/app.py', fraction: 0.5 },
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
    expect(el.querySelectorAll('.ct-agent').length).toBe(3);
    expect(el.querySelector('.ct-monitor__activity-bar')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);
});
