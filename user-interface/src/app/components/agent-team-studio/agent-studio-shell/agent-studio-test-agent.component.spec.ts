import { Component, EventEmitter, Input, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { STAGE_INDEX } from '../../../models/agent-studio.model';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentRunnerComponent } from '../agent-console/agent-runner/agent-runner.component';
import { AgentStudioTestAgentComponent } from './agent-studio-test-agent.component';

/**
 * Lightweight stand-in for the heavy Agent Console runner: same selector and the
 * single input/output Stage 2 wires, so the runner's sandbox polling, HTTP, and
 * dialogs never run in these unit tests.
 */
@Component({ selector: 'app-agent-runner', standalone: true, template: '' })
class StubAgentRunnerComponent {
  @Input() preselectedAgentId: string | null = null;
  @Output() readonly requestCatalogReturn = new EventEmitter<void>();
}

describe('AgentStudioTestAgentComponent', () => {
  let fixture: ComponentFixture<AgentStudioTestAgentComponent>;
  let component: AgentStudioTestAgentComponent;
  let state: AgentStudioStateService;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AgentStudioTestAgentComponent, NoopAnimationsModule],
      providers: [AgentStudioStateService],
    })
      .overrideComponent(AgentStudioTestAgentComponent, {
        remove: { imports: [AgentRunnerComponent] },
        add: { imports: [StubAgentRunnerComponent] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(AgentStudioTestAgentComponent);
    component = fixture.componentInstance;
    state = TestBed.inject(AgentStudioStateService);
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('shows the empty state and mounts no runner when no agent is selected', () => {
    expect(component.agentId()).toBeNull();
    expect(fixture.nativeElement.querySelector('app-agent-runner')).toBeNull();
    const empty = fixture.nativeElement.querySelector('.studio-test__empty');
    expect(empty).toBeTruthy();
    expect(empty.textContent).toContain('No agent selected');
  });

  it('renders the runner pre-seeded with the handoff agent id when one is selected', () => {
    state.setRegistryAgentId('blogging.planner');
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.studio-test__empty')).toBeNull();
    expect(fixture.nativeElement.querySelector('.studio-test__agent').textContent).toContain(
      'blogging.planner',
    );
    const runner = fixture.debugElement.query(By.directive(StubAgentRunnerComponent));
    expect(runner).toBeTruthy();
    expect((runner.componentInstance as StubAgentRunnerComponent).preselectedAgentId).toBe(
      'blogging.planner',
    );
  });

  it('empty-state "Back to Build" returns the stepper to Stage 1 (Build)', () => {
    state.navigateToStage(STAGE_INDEX.test); // on Test with no agent → empty state
    fixture.detectChanges();
    fixture.nativeElement.querySelector('.studio-test__empty button').click();
    expect(state.activeStage()).toBe(0);
  });

  it('runner "back to catalog" returns the stepper to Stage 1 (Build)', () => {
    state.navigateToStage(STAGE_INDEX.test);
    state.setRegistryAgentId('soc2.auditor');
    fixture.detectChanges();

    const runner = fixture.debugElement.query(By.directive(StubAgentRunnerComponent));
    (runner.componentInstance as StubAgentRunnerComponent).requestCatalogReturn.emit();
    expect(state.activeStage()).toBe(0);
  });
});
