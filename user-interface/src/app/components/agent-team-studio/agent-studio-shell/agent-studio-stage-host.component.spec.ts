import { Component, EventEmitter, Input, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { AgentRunnerApiService } from '../../../services/agent-runner-api.service';
import { AgentStudioApiService } from '../../../services/agent-studio-api.service';
import { AgentStudioFacade } from '../../../services/agent-studio.facade';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgenticTeamApiService } from '../../../services/agentic-team-api.service';
import { PersonaTestingApiService } from '../../../services/persona-testing-api.service';
import { AgentCatalogComponent } from '../agent-console/agent-catalog/agent-catalog.component';
import { AgentRunnerComponent } from '../agent-console/agent-runner/agent-runner.component';
import { AgentProvisioningPanelComponent } from '../agent-provisioning-panel/agent-provisioning-panel.component';
import { AgentStudioBuildAgentComponent } from './agent-studio-build-agent.component';
import { AgentStudioComposeTeamComponent } from './agent-studio-compose-team.component';
import { AgentStudioPersonaComponent } from './agent-studio-persona.component';
import { AgentStudioStageHostComponent } from './agent-studio-stage-host.component';
import { AgentStudioTestAgentComponent } from './agent-studio-test-agent.component';

/** Stub the heavy Agent Console runner so the Test stage can mount with an agent
 *  set without firing sandbox polling / HTTP inside the stage-host tests. */
@Component({ selector: 'app-agent-runner', standalone: true, template: '' })
class StubAgentRunnerComponent {
  @Input() preselectedAgentId: string | null = null;
  @Output() readonly requestCatalogReturn = new EventEmitter<void>();
}

/** Stub the catalog + provisioning panel so the Build stage (the default
 *  active stage) mounts without firing catalog HTTP / provisioning polling. */
@Component({ selector: 'app-agent-catalog', standalone: true, template: '' })
class StubAgentCatalogComponent {
  @Output() readonly requestRun = new EventEmitter<string>();
}

@Component({ selector: 'app-agent-provisioning-panel', standalone: true, template: '' })
class StubAgentProvisioningPanelComponent {}

/** Stub the Stage-4 persona component so the host's Personas-stage tests don't pull
 *  in its API services / dialog. */
@Component({ selector: 'app-agent-studio-persona', standalone: true, template: '' })
class StubPersonaComponent {}

/** Stub the Stage-3 compose component so the host's Compose-stage tests don't
 *  pull in its API services / the embedded process-designer-chat. */
@Component({ selector: 'app-agent-studio-compose-team', standalone: true, template: '' })
class StubComposeTeamComponent {}

describe('AgentStudioStageHostComponent', () => {
  let fixture: ComponentFixture<AgentStudioStageHostComponent>;
  let state: AgentStudioStateService;
  let agentStudioApi: {
    cloneFromRegistry: ReturnType<typeof vi.fn>;
    saveAgent: ReturnType<typeof vi.fn>;
    listDrafts: ReturnType<typeof vi.fn>;
    getDraft: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    agentStudioApi = {
      cloneFromRegistry: vi.fn().mockReturnValue(of({})),
      saveAgent: vi.fn().mockReturnValue(of({})),
      listDrafts: vi.fn().mockReturnValue(of([])),
      getDraft: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [AgentStudioStageHostComponent, NoopAnimationsModule],
      providers: [
        AgentStudioStateService,
        AgentStudioFacade,
        { provide: AgentStudioApiService, useValue: agentStudioApi },
        { provide: AgentRunnerApiService, useValue: {} },
        { provide: AgenticTeamApiService, useValue: {} },
        { provide: PersonaTestingApiService, useValue: {} },
      ],
    })
      .overrideComponent(AgentStudioTestAgentComponent, {
        remove: { imports: [AgentRunnerComponent] },
        add: { imports: [StubAgentRunnerComponent] },
      })
      .overrideComponent(AgentStudioBuildAgentComponent, {
        remove: { imports: [AgentCatalogComponent, AgentProvisioningPanelComponent] },
        add: { imports: [StubAgentCatalogComponent, StubAgentProvisioningPanelComponent] },
      })
      .overrideComponent(AgentStudioStageHostComponent, {
        remove: { imports: [AgentStudioPersonaComponent, AgentStudioComposeTeamComponent] },
        add: { imports: [StubPersonaComponent, StubComposeTeamComponent] },
      })
      .compileComponents();

    state = TestBed.inject(AgentStudioStateService);
    fixture = TestBed.createComponent(AgentStudioStageHostComponent);
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('renders the real Build Agent stage (not the placeholder) when activeStage is 0', () => {
    expect(state.activeStage()).toBe(0);
    expect(fixture.nativeElement.querySelector('app-agent-studio-build-agent')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeNull();
  });

  it('renders the real Test Agent stage (not the placeholder) when activeStage is 1', () => {
    state.navigateToStage(1);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('app-agent-studio-test-agent')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeNull();
  });

  it('renders the real Compose Team stage (not the placeholder) when activeStage is 2', () => {
    state.navigateToStage(2);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('app-agent-studio-compose-team')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeNull();
  });

  it('renders the Personas stage when activeStage is 3', () => {
    state.navigateToStage(3);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('app-agent-studio-persona')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeNull();
  });
});
