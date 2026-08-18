import { Component, Input } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { PersonaTestAuditPanelComponent } from '../persona-test-audit-panel/persona-test-audit-panel.component';
import { AgentStudioPersonaAuditComponent } from './agent-studio-persona-audit.component';

@Component({ selector: 'app-persona-test-audit-panel', standalone: true, template: '' })
class StubAuditPanelComponent {
  @Input() backLink = '/persona-testing';
  @Input() backLabel = 'Back to Testing Personas';
}

describe('AgentStudioPersonaAuditComponent', () => {
  let fixture: ComponentFixture<AgentStudioPersonaAuditComponent>;
  let state: AgentStudioStateService;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AgentStudioPersonaAuditComponent],
      providers: [AgentStudioStateService],
    })
      .overrideComponent(AgentStudioPersonaAuditComponent, {
        remove: { imports: [PersonaTestAuditPanelComponent] },
        add: { imports: [StubAuditPanelComponent] },
      })
      .compileComponents();

    state = TestBed.inject(AgentStudioStateService);
    state.setRegistryAgentId('blogging.planner');
    fixture = TestBed.createComponent(AgentStudioPersonaAuditComponent);
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('moves the stepper to Personas (index 3) on init', () => {
    expect(state.activeStage()).toBe(3);
  });

  it('does not clear existing handoff state', () => {
    expect(state.registryAgentId()).toBe('blogging.planner');
  });

  it('mounts the audit panel with Studio back inputs', () => {
    const panel = fixture.debugElement.query(By.directive(StubAuditPanelComponent));
    expect(panel).toBeTruthy();
    const stub = panel.componentInstance as StubAuditPanelComponent;
    expect(stub.backLink).toBe('/agent-studio');
    expect(stub.backLabel).toBe('Back to Agent Studio');
  });
});
