import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { AgentStudioFacade } from '../../../services/agent-studio.facade';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentStudioPersonaComponent } from './agent-studio-persona.component';
import { expectNoAxeViolations } from '../../../testing/a11y';

/**
 * A11y audit of Stage 4's richest state: the live persona-run view — sub-mode
 * tablist, the (disabled-while-live) launcher, the labelled live-run region with
 * status chip + Stop control + step progress bar, and the decision transcript.
 *
 * `page-has-heading-one` / `region` are disabled: this is a *stage fragment*
 * mounted inside the Agent Studio shell, which owns the page `<h1>` and the
 * `<main>` landmark — neither exists when the fragment is rendered in isolation.
 */
describe('AgentStudioPersonaComponent a11y', () => {
  const STEPS = [
    { step_id: 's1', name: 'Plan', description: '', step_type: 'action', agents: [], next_steps: [] },
    { step_id: 's2', name: 'Write', description: '', step_type: 'action', agents: [], next_steps: [] },
  ];
  const TEAM = {
    team_id: 't1',
    name: 'Growth Pod',
    description: '',
    agents: [],
    processes: [
      { process_id: 'p1', name: 'Content pipeline', description: '', steps: STEPS, status: 'complete' },
    ],
    created_at: '',
    updated_at: '',
  };
  const PIPELINE_RUN = {
    run_id: 'pipe-1',
    team_id: 't1',
    process_id: 'p1',
    status: 'running',
    current_step_id: 's2',
    initial_input: null,
    step_results: [
      { step_id: 's1', step_name: '', agent_name: '', input: '', output: '', status: 'completed' },
    ],
    human_prompt: null,
    error: null,
    started_at: '',
    finished_at: null,
  };

  const setup = async () => {
    const facade = {
      getTeam: vi.fn().mockReturnValue(of({ team: TEAM })),
      getTeamPipelineRun: vi.fn().mockReturnValue(of(PIPELINE_RUN)),
      listPersonas: vi.fn().mockReturnValue(
        of({
          personas: [
            { id: 'startup-founder', name: 'Startup Founder', description: '', icon: 'rocket', is_builtin: true },
          ],
        }),
      ),
      startPersonaRun: vi.fn().mockReturnValue(of({ job_id: 'run-1', status: 'running', message: '' })),
      getPersonaRunStatus: vi.fn().mockReturnValue(
        of({
          run_id: 'run-1',
          status: 'polling_build',
          se_job_id: 'pipe-1',
          decisions: [
            {
              decision_id: 1,
              question_text: 'Which tone for the post?',
              answer_text: 'Punchy, founder-voice',
              rationale: 'Matches the audience',
              timestamp: '',
            },
          ],
        }),
      ),
      cancelPersonaRun: vi.fn().mockReturnValue(of({})),
    };
    await TestBed.configureTestingModule({
      imports: [AgentStudioPersonaComponent, NoopAnimationsModule],
      providers: [
        AgentStudioStateService,
        { provide: AgentStudioFacade, useValue: facade },
      ],
    }).compileComponents();
    TestBed.inject(AgentStudioStateService).setTeamId('t1');
  };

  it('has no axe violations in the live persona-run view', async () => {
    await setup();
    const fixture = TestBed.createComponent(AgentStudioPersonaComponent);
    fixture.detectChanges(); // loads team + personas → launcher
    // pollWhile's immediate poll fires via a timer(0); flush it with fake
    // timers so the run status lands before rendering is asserted below.
    vi.useFakeTimers();
    fixture.componentInstance.launch(); // start a run → live-run panel
    vi.advanceTimersByTime(0);
    vi.useRealTimers();
    fixture.detectChanges();

    // Guard: the rich live-run state actually rendered before auditing.
    expect(fixture.nativeElement.querySelector('section.persona__run')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.persona__stop')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('mat-progress-bar')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.persona__decisions')).toBeTruthy();

    await expectNoAxeViolations(fixture.nativeElement, {
      'page-has-heading-one': { enabled: false },
      region: { enabled: false },
    });
  });
});
