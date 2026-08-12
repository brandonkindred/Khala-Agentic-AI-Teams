import { Component, EventEmitter, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { Subject, of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentStudioApiService } from '../../../services/agent-studio-api.service';
import { AgentCatalogComponent } from '../agent-console/agent-catalog/agent-catalog.component';
import { AgentProvisioningPanelComponent } from '../agent-provisioning-panel/agent-provisioning-panel.component';
import { AgentStudioBuildAgentComponent } from './agent-studio-build-agent.component';
import type { AgentDefinition } from '../../../models/agent-studio.model';

/** Stand-in for the catalog: same selector + the one output Stage 1 wires, so
 *  no catalog HTTP fetch runs in these unit tests. */
@Component({ selector: 'app-agent-catalog', standalone: true, template: '' })
class StubAgentCatalogComponent {
  @Output() readonly requestRun = new EventEmitter<string>();
}

/** Stand-in for the provisioning panel (embeds team-assistant-chat + polls),
 *  so opening the Stage-1 slide-out doesn't start real chat HTTP / job
 *  polling in these shell-level tests. */
@Component({ selector: 'app-agent-provisioning-panel', standalone: true, template: '' })
class StubAgentProvisioningPanelComponent {}

const definition = (overrides: Partial<AgentDefinition> = {}): AgentDefinition => ({
  name: 'blogging.planner.v2',
  role: 'Plans SEO-aware outlines',
  description: null,
  tags: [],
  tools: [],
  system_prompt: '',
  input_schema: null,
  output_schema: null,
  states: [],
  mode: 'refine',
  cloned_from: 'blogging.planner',
  ...overrides,
});

describe('AgentStudioBuildAgentComponent', () => {
  let fixture: ComponentFixture<AgentStudioBuildAgentComponent>;
  let component: AgentStudioBuildAgentComponent;
  let state: AgentStudioStateService;
  let api: {
    cloneFromRegistry: ReturnType<typeof vi.fn>;
    saveAgent: ReturnType<typeof vi.fn>;
  };

  function configure(): void {
    TestBed.configureTestingModule({
      imports: [AgentStudioBuildAgentComponent, NoopAnimationsModule],
      providers: [AgentStudioStateService, { provide: AgentStudioApiService, useValue: api }],
    })
      .overrideComponent(AgentStudioBuildAgentComponent, {
        remove: { imports: [AgentCatalogComponent, AgentProvisioningPanelComponent] },
        add: { imports: [StubAgentCatalogComponent, StubAgentProvisioningPanelComponent] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(AgentStudioBuildAgentComponent);
    component = fixture.componentInstance;
    state = TestBed.inject(AgentStudioStateService);
    fixture.detectChanges();
  }

  function selectAgent(id = 'blogging.planner'): void {
    const catalog = fixture.debugElement.query(By.directive(StubAgentCatalogComponent));
    (catalog.componentInstance as StubAgentCatalogComponent).requestRun.emit(id);
    fixture.detectChanges();
  }

  beforeEach(async () => {
    api = {
      cloneFromRegistry: vi.fn().mockReturnValue(of(definition())),
      saveAgent: vi.fn().mockReturnValue(of({ agent_id: 'blogging.planner.v2', manifest: {}, created: true })),
    };
    configure();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('shows the pick-an-agent hint and embeds the catalog before any selection', () => {
    expect(component.selectedAgentId()).toBeNull();
    expect(fixture.nativeElement.querySelector('app-agent-catalog')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.studio-build__hint')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.studio-build__selected')).toBeNull();
  });

  it('clones the catalog selection via AgentStudioApiService and shows the cloned bar', () => {
    selectAgent('blogging.planner');

    expect(api.cloneFromRegistry).toHaveBeenCalledWith('blogging.planner');
    expect(component.draftDefinition()).toEqual(definition());
    expect(state.registryAgentId()).toBeNull();
    expect(state.draftAgentId()).toEqual(expect.any(String));
    expect(state.draftAgentId()).not.toBe('');
    const selected = fixture.nativeElement.querySelector('.studio-build__selected');
    expect(selected).toBeTruthy();
    expect(selected.textContent).toContain('blogging.planner');
    expect(fixture.nativeElement.querySelector('.studio-build__hint')).toBeNull();
  });

  it('surfaces a clone failure without breaking the sub-stepper, and allows retry', () => {
    api.cloneFromRegistry.mockReturnValueOnce(throwError(() => ({ error: { detail: 'source agent missing' } })));
    selectAgent('blogging.planner');

    expect(component.cloneError()).toBe('source agent missing');
    expect(component.draftDefinition()).toBeNull();
    expect(state.draftAgentId()).toBeNull();
    expect(fixture.nativeElement.querySelector('.error-text').textContent).toContain('source agent missing');
    expect(fixture.nativeElement.querySelector('.studio-build__continue-sub')).toBeNull();

    selectAgent('blogging.planner');
    expect(component.cloneError()).toBeNull();
    expect(component.draftDefinition()).toEqual(definition());
    expect(state.draftAgentId()).toEqual(expect.any(String));
  });

  it('falls back to a generic message when a clone error has no detail', () => {
    api.cloneFromRegistry.mockReturnValueOnce(throwError(() => ({})));
    selectAgent('blogging.planner');
    expect(component.cloneError()).toBe('Could not clone this agent — try again.');
  });

  it('ignores a repeat clone request while one is already in flight', () => {
    const pending = new Subject<AgentDefinition>();
    api.cloneFromRegistry.mockReturnValue(pending.asObservable());

    component.onSelectAgent('blogging.planner');
    component.onSelectAgent('blogging.planner');
    expect(api.cloneFromRegistry).toHaveBeenCalledTimes(1);

    pending.next(definition());
    pending.complete();
    expect(component.draftDefinition()).toEqual(definition());
  });

  it('does not save without a cloned draft', () => {
    component.saveAgent();
    expect(api.saveAgent).not.toHaveBeenCalled();
  });

  it('keeps the provisioning slide-out closed until requested', () => {
    expect(component.provisionOpen()).toBe(false);
    expect(fixture.nativeElement.querySelector('app-agent-provisioning-panel')).toBeNull();
  });

  it('opens and closes the provisioning slide-out', () => {
    fixture.nativeElement.querySelector('.studio-build__provision-btn').click();
    fixture.detectChanges();
    expect(component.provisionOpen()).toBe(true);
    expect(fixture.nativeElement.querySelector('app-agent-provisioning-panel')).toBeTruthy();

    fixture.nativeElement.querySelector('.studio-build__provision-head button').click();
    fixture.detectChanges();
    expect(component.provisionOpen()).toBe(false);
    expect(fixture.nativeElement.querySelector('app-agent-provisioning-panel')).toBeNull();
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

    it('hides the Continue-to-Define action until an agent is cloned', () => {
      expect(fixture.nativeElement.querySelector('.studio-build__continue-sub')).toBeNull();

      selectAgent('blogging.planner');

      expect(fixture.nativeElement.querySelector('.studio-build__continue-sub')).toBeTruthy();
    });

    it('advances Start → Define → Configure via the explicit Continue actions, and back via ◂ back to Define', () => {
      selectAgent('blogging.planner');
      // Start: only the forward Continue action is present — no back/save affordance to skip ahead with.
      expect(fixture.nativeElement.querySelector('.studio-build__continue-sub')).toBeTruthy();
      expect(fixture.nativeElement.querySelector('.studio-build__back-sub')).toBeNull();
      expect(fixture.nativeElement.querySelector('.studio-build__save-sub')).toBeNull();

      fixture.nativeElement.querySelector('.studio-build__continue-sub').click();
      fixture.detectChanges();
      expect(component.activeSubStageDef().key).toBe('define');
      expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeTruthy();
      expect(fixture.nativeElement.querySelector('app-agent-catalog')).toBeNull();
      // Define: still only the forward Continue action — no back/save affordance yet.
      expect(fixture.nativeElement.querySelector('.studio-build__continue-sub')).toBeTruthy();
      expect(fixture.nativeElement.querySelector('.studio-build__back-sub')).toBeNull();
      expect(fixture.nativeElement.querySelector('.studio-build__save-sub')).toBeNull();

      fixture.nativeElement.querySelector('.studio-build__continue-sub').click();
      fixture.detectChanges();
      expect(component.activeSubStageDef().key).toBe('configure');
      expect(state.maxReachedBuildSubStage()).toBe(2);
      // Configure: the forward Continue action is gone — back and save are the only actions.
      expect(fixture.nativeElement.querySelector('.studio-build__continue-sub')).toBeNull();
      expect(fixture.nativeElement.querySelector('.studio-build__back-sub')).toBeTruthy();
      expect(fixture.nativeElement.querySelector('.studio-build__save-sub')).toBeTruthy();

      fixture.nativeElement.querySelector('.studio-build__back-sub').click();
      fixture.detectChanges();
      expect(component.activeSubStageDef().key).toBe('define');
      // Explicit back-loop, not a reset — the furthest sub-stage reached is preserved.
      expect(state.maxReachedBuildSubStage()).toBe(2);
    });

    it('rejects a forward-only violation instead of swallowing it — backToDefine() off the Configure sub-stage throws', () => {
      expect(component.activeSubStageDef().key).toBe('start');
      expect(() => component.backToDefine()).toThrow(RangeError);
      // The rejected call left the sub-stepper untouched.
      expect(component.activeSubStageDef().key).toBe('start');

      selectAgent('blogging.planner');
      fixture.nativeElement.querySelector('.studio-build__continue-sub').click();
      fixture.detectChanges();
      expect(component.activeSubStageDef().key).toBe('define');
      expect(() => component.backToDefine()).toThrow(RangeError);
      expect(component.activeSubStageDef().key).toBe('define');
    });

    it('shows the "Cloning agent…" hint only while a clone is in flight', () => {
      const pending = new Subject<AgentDefinition>();
      api.cloneFromRegistry.mockReturnValue(pending.asObservable());

      component.onSelectAgent('blogging.planner');
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.studio-build__hint')?.textContent).toContain('Cloning agent…');
      expect(fixture.nativeElement.querySelector('.studio-build__selected')).toBeNull();

      pending.next(definition());
      pending.complete();
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.studio-build__hint')).toBeNull();
      expect(fixture.nativeElement.querySelector('.studio-build__selected')).toBeTruthy();
    });

    it('marks only the active sub-step with aria-current="step"', () => {
      const steps = fixture.nativeElement.querySelectorAll('.studio-build__substep');
      expect(steps[0].getAttribute('aria-current')).toBe('step');
      expect(steps[1].getAttribute('aria-current')).toBeNull();

      selectAgent('blogging.planner');
      fixture.nativeElement.querySelector('.studio-build__continue-sub').click();
      fixture.detectChanges();

      expect(steps[0].getAttribute('aria-current')).toBeNull();
      expect(steps[1].getAttribute('aria-current')).toBe('step');
    });
  });

  describe('1.3 Configure — save + register', () => {
    function goToConfigure(): void {
      selectAgent('blogging.planner');
      fixture.nativeElement.querySelector('.studio-build__continue-sub').click();
      fixture.detectChanges();
      fixture.nativeElement.querySelector('.studio-build__continue-sub').click();
      fixture.detectChanges();
    }

    it('saves and registers the draft via AgentStudioApiService, unlocking the journey gate', () => {
      goToConfigure();
      const draftAgentIdBeforeSave = state.draftAgentId();

      fixture.nativeElement.querySelector('.studio-build__save-sub').click();
      fixture.detectChanges();

      expect(api.saveAgent).toHaveBeenCalledWith({
        name: 'blogging.planner.v2',
        role: 'Plans SEO-aware outlines',
        description: null,
        tags: [],
        tools: [],
        system_prompt: '',
        input_schema: null,
        output_schema: null,
        states: [],
      });
      expect(state.registryAgentId()).toBe('blogging.planner.v2');
      expect(component.saveError()).toBeNull();
      // Save doesn't clobber the build-session bookkeeping id.
      expect(state.draftAgentId()).toBe(draftAgentIdBeforeSave);
    });

    it('surfaces a save failure without leaving the sub-stepper in a broken state', () => {
      api.saveAgent.mockReturnValueOnce(throwError(() => ({ error: { detail: 'name already taken' } })));
      goToConfigure();

      fixture.nativeElement.querySelector('.studio-build__save-sub').click();
      fixture.detectChanges();

      expect(component.saveError()).toBe('name already taken');
      expect(state.registryAgentId()).toBeNull();
      expect(component.activeSubStageDef().key).toBe('configure');
      expect(component.draftDefinition()).toEqual(definition());
      expect(fixture.nativeElement.querySelector('.error-text').textContent).toContain('name already taken');

      // Retry succeeds without re-cloning or losing the draft.
      fixture.nativeElement.querySelector('.studio-build__save-sub').click();
      fixture.detectChanges();
      expect(state.registryAgentId()).toBe('blogging.planner.v2');
      expect(component.saveError()).toBeNull();
    });

    it('falls back to a generic message when a save error has no detail', () => {
      api.saveAgent.mockReturnValueOnce(throwError(() => ({})));
      goToConfigure();

      fixture.nativeElement.querySelector('.studio-build__save-sub').click();
      fixture.detectChanges();

      expect(component.saveError()).toBe('Could not save this agent — try again.');
    });

    it('ignores a repeat save request while one is already in flight', () => {
      const pending = new Subject<{ agent_id: string; manifest: unknown; created: boolean }>();
      api.saveAgent.mockReturnValue(pending.asObservable());
      goToConfigure();

      component.saveAgent();
      component.saveAgent();
      expect(api.saveAgent).toHaveBeenCalledTimes(1);

      pending.next({ agent_id: 'blogging.planner.v2', manifest: {}, created: true });
      pending.complete();
      expect(state.registryAgentId()).toBe('blogging.planner.v2');
    });

    it('disables the Save button and relabels it "Saving…" while the save is in flight, then re-enables it', () => {
      const pending = new Subject<{ agent_id: string; manifest: unknown; created: boolean }>();
      api.saveAgent.mockReturnValue(pending.asObservable());
      goToConfigure();

      const saveButton = (): HTMLButtonElement => fixture.nativeElement.querySelector('.studio-build__save-sub');
      expect(saveButton().disabled).toBe(false);
      expect(saveButton().textContent?.trim()).toBe('Save agent');

      saveButton().click();
      fixture.detectChanges();
      expect(saveButton().disabled).toBe(true);
      expect(saveButton().textContent?.trim()).toBe('Saving…');

      pending.next({ agent_id: 'blogging.planner.v2', manifest: {}, created: true });
      pending.complete();
      fixture.detectChanges();
      expect(saveButton().disabled).toBe(false);
      expect(saveButton().textContent?.trim()).toBe('Save agent');
    });
  });
});
