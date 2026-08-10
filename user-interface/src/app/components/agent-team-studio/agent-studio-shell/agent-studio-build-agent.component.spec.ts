import { Component, EventEmitter, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentCatalogComponent } from '../agent-console/agent-catalog/agent-catalog.component';
import { AgentProvisionSlideOutComponent } from '../agent-provision-slide-out/agent-provision-slide-out.component';
import { AgentStudioBuildAgentComponent } from './agent-studio-build-agent.component';

/** Stand-in for the catalog: same selector + the one output Stage 1 wires, so
 *  no catalog HTTP fetch runs in these unit tests. */
@Component({ selector: 'app-agent-catalog', standalone: true, template: '' })
class StubAgentCatalogComponent {
  @Output() readonly requestRun = new EventEmitter<string>();
}

/** Stand-in for the provision slide-out (embeds team-assistant-chat + polls),
 *  so opening the Stage-1 slide-out doesn't start real chat HTTP / job
 *  polling in these shell-level tests. */
@Component({ selector: 'app-agent-provision-slide-out', standalone: true, template: '' })
class StubProvisionSlideOutComponent {}

describe('AgentStudioBuildAgentComponent', () => {
  let fixture: ComponentFixture<AgentStudioBuildAgentComponent>;
  let component: AgentStudioBuildAgentComponent;
  let state: AgentStudioStateService;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AgentStudioBuildAgentComponent, NoopAnimationsModule],
      providers: [AgentStudioStateService],
    })
      .overrideComponent(AgentStudioBuildAgentComponent, {
        remove: { imports: [AgentCatalogComponent, AgentProvisionSlideOutComponent] },
        add: { imports: [StubAgentCatalogComponent, StubProvisionSlideOutComponent] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(AgentStudioBuildAgentComponent);
    component = fixture.componentInstance;
    state = TestBed.inject(AgentStudioStateService);
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('shows the pick-an-agent hint and embeds the catalog before any selection', () => {
    expect(component.selectedAgentId()).toBeNull();
    expect(fixture.nativeElement.querySelector('app-agent-catalog')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.studio-build__hint')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.studio-build__selected')).toBeNull();
  });

  it('records the catalog selection as the journey agent and shows the selected bar', () => {
    const catalog = fixture.debugElement.query(By.directive(StubAgentCatalogComponent));
    (catalog.componentInstance as StubAgentCatalogComponent).requestRun.emit('blogging.planner');
    fixture.detectChanges();

    expect(state.registryAgentId()).toBe('blogging.planner');
    const selected = fixture.nativeElement.querySelector('.studio-build__selected');
    expect(selected).toBeTruthy();
    expect(selected.textContent).toContain('blogging.planner');
    expect(fixture.nativeElement.querySelector('.studio-build__hint')).toBeNull();
  });

  it('keeps the provisioning slide-out closed until requested', () => {
    expect(component.provisionOpen()).toBe(false);
    expect(fixture.nativeElement.querySelector('app-agent-provision-slide-out')).toBeNull();
  });

  it('opens and closes the provisioning slide-out', () => {
    fixture.nativeElement.querySelector('.studio-build__provision-btn').click();
    fixture.detectChanges();
    expect(component.provisionOpen()).toBe(true);
    expect(fixture.nativeElement.querySelector('app-agent-provision-slide-out')).toBeTruthy();

    fixture.nativeElement.querySelector('.studio-build__provision-head button').click();
    fixture.detectChanges();
    expect(component.provisionOpen()).toBe(false);
    expect(fixture.nativeElement.querySelector('app-agent-provision-slide-out')).toBeNull();
  });

  it('closes the provisioning slide-out when the scrim is clicked', () => {
    component.openProvision();
    fixture.detectChanges();
    fixture.nativeElement.querySelector('.studio-build__scrim').click();
    fixture.detectChanges();
    expect(component.provisionOpen()).toBe(false);
  });

  it('marks the provisioning panel as a focus-trapping modal', () => {
    component.openProvision();
    fixture.detectChanges();
    const panel = fixture.nativeElement.querySelector('.studio-build__provision-panel');
    expect(panel.getAttribute('aria-modal')).toBe('true');
    expect(panel.getAttribute('role')).toBe('dialog');
    // CDK focus trap directive is applied (keeps Tab inside the modal).
    expect(panel.hasAttribute('cdkTrapFocus')).toBe(true);
  });

  it('closes the provisioning slide-out on Escape', () => {
    component.openProvision();
    fixture.detectChanges();
    const panel = fixture.nativeElement.querySelector('.studio-build__provision-panel');
    panel.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    fixture.detectChanges();
    expect(component.provisionOpen()).toBe(false);
  });

  describe('1.1-1.3 sub-stepper', () => {
    it('renders one sub-step indicator per sub-stage with Start active', () => {
      const steps = fixture.nativeElement.querySelectorAll('.studio-build__substep');
      expect(steps.length).toBe(3);
      expect(steps[0].classList.contains('is-active')).toBe(true);
      expect(steps[1].classList.contains('is-active')).toBe(false);
      expect(steps[2].classList.contains('is-active')).toBe(false);
      expect(component.activeSubStageDef().key).toBe('start');
    });

    it('sub-step indicators are not buttons — there is no backward or skip navigation', () => {
      const steps = fixture.nativeElement.querySelectorAll('.studio-build__substep');
      for (const step of Array.from(steps)) {
        expect((step as HTMLElement).tagName).toBe('DIV');
      }
      expect(fixture.nativeElement.querySelector('.studio-build__substepper button')).toBeNull();
    });

    it('hides the Continue-to-Define action until an agent is selected', () => {
      expect(fixture.nativeElement.querySelector('.studio-build__continue-sub')).toBeNull();

      const catalog = fixture.debugElement.query(By.directive(StubAgentCatalogComponent));
      (catalog.componentInstance as StubAgentCatalogComponent).requestRun.emit('blogging.planner');
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.studio-build__continue-sub')).toBeTruthy();
    });

    it('advances Start → Define → Configure via the explicit Continue actions, and back via ◂ back to Define', () => {
      state.setRegistryAgentId('blogging.planner');
      fixture.detectChanges();

      fixture.nativeElement.querySelector('.studio-build__continue-sub').click();
      fixture.detectChanges();
      expect(component.activeSubStageDef().key).toBe('define');
      expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeTruthy();
      expect(fixture.nativeElement.querySelector('app-agent-catalog')).toBeNull();

      fixture.nativeElement.querySelector('.studio-build__continue-sub').click();
      fixture.detectChanges();
      expect(component.activeSubStageDef().key).toBe('configure');
      expect(state.maxReachedBuildSubStage()).toBe(2);
      expect(fixture.nativeElement.querySelector('.studio-build__continue-sub')).toBeNull();

      fixture.nativeElement.querySelector('.studio-build__back-sub').click();
      fixture.detectChanges();
      expect(component.activeSubStageDef().key).toBe('define');
      // Explicit back-loop, not a reset — the furthest sub-stage reached is preserved.
      expect(state.maxReachedBuildSubStage()).toBe(2);
    });

    it('marks only the active sub-step with aria-current="step"', () => {
      const steps = fixture.nativeElement.querySelectorAll('.studio-build__substep');
      expect(steps[0].getAttribute('aria-current')).toBe('step');
      expect(steps[1].getAttribute('aria-current')).toBeNull();

      state.setRegistryAgentId('blogging.planner');
      fixture.detectChanges();
      fixture.nativeElement.querySelector('.studio-build__continue-sub').click();
      fixture.detectChanges();

      expect(steps[0].getAttribute('aria-current')).toBeNull();
      expect(steps[1].getAttribute('aria-current')).toBe('step');
    });
  });
});
