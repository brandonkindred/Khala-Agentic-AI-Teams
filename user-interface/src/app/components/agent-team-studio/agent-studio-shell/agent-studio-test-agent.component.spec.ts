import { Component, EventEmitter, Input, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { STAGE_INDEX } from '../../../models/agent-studio.model';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentCatalogComponent } from '../agent-console/agent-catalog/agent-catalog.component';
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

/** Stand-in for the catalog: same selector + the one output the Browse-agents
 *  slide-out wires, so no catalog HTTP fetch runs in these unit tests. */
@Component({ selector: 'app-agent-catalog', standalone: true, template: '' })
class StubAgentCatalogComponent {
  @Output() readonly requestRun = new EventEmitter<string>();
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
        remove: { imports: [AgentRunnerComponent, AgentCatalogComponent] },
        add: { imports: [StubAgentRunnerComponent, StubAgentCatalogComponent] },
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

  describe('Browse agents slide-out', () => {
    beforeEach(() => {
      state.setRegistryAgentId('blogging.planner');
      fixture.detectChanges();
    });

    it('keeps the slide-out closed until requested', () => {
      expect(component.browseOpen()).toBe(false);
      expect(fixture.nativeElement.querySelector('app-agent-catalog')).toBeNull();
    });

    it('opens and closes the slide-out via its buttons', () => {
      fixture.nativeElement.querySelector('.studio-test__browse-btn').click();
      fixture.detectChanges();
      expect(component.browseOpen()).toBe(true);
      expect(fixture.nativeElement.querySelector('app-agent-catalog')).toBeTruthy();

      fixture.nativeElement.querySelector('.studio-test__browse-head button').click();
      fixture.detectChanges();
      expect(component.browseOpen()).toBe(false);
      expect(fixture.nativeElement.querySelector('app-agent-catalog')).toBeNull();
    });

    it('closes the slide-out when the scrim is clicked', () => {
      component.openBrowse();
      fixture.detectChanges();
      fixture.nativeElement.querySelector('.studio-test__scrim').click();
      fixture.detectChanges();
      expect(component.browseOpen()).toBe(false);
    });

    it('closes the slide-out on Escape', () => {
      component.openBrowse();
      fixture.detectChanges();
      const panel = fixture.nativeElement.querySelector('.studio-test__browse-panel');
      panel.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      fixture.detectChanges();
      expect(component.browseOpen()).toBe(false);
    });

    it('marks the panel as a focus-trapping modal', () => {
      component.openBrowse();
      fixture.detectChanges();
      const panel = fixture.nativeElement.querySelector('.studio-test__browse-panel');
      expect(panel.getAttribute('aria-modal')).toBe('true');
      expect(panel.getAttribute('role')).toBe('dialog');
      expect(panel.hasAttribute('cdkTrapFocus')).toBe(true);
    });

    it('selecting an agent re-points the handoff id and closes the slide-out', () => {
      component.openBrowse();
      fixture.detectChanges();
      const catalog = fixture.debugElement.query(By.directive(StubAgentCatalogComponent));
      (catalog.componentInstance as StubAgentCatalogComponent).requestRun.emit('soc2.auditor');
      fixture.detectChanges();

      expect(state.registryAgentId()).toBe('soc2.auditor');
      expect(component.browseOpen()).toBe(false);
      expect(fixture.nativeElement.querySelector('.studio-test__agent').textContent).toContain(
        'soc2.auditor',
      );
    });
  });
});
