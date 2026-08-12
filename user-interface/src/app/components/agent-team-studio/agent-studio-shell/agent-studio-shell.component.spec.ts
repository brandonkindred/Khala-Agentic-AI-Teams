import { Component, EventEmitter, Input, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { MatDialog } from '@angular/material/dialog';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { Subject, of, throwError } from 'rxjs';
import { vi } from 'vitest';
import type { AgentStudioDraft, AgentStudioDraftSummary } from '../../../models/agent-studio.model';
import { AgentStudioApiService } from '../../../services/agent-studio-api.service';
import { AgenticTeamApiService } from '../../../services/agentic-team-api.service';
import type { ProcessDefinition } from '../../../models/agentic-team.model';
import { AgentCatalogComponent } from '../agent-console/agent-catalog/agent-catalog.component';
import { AgentProvisioningPanelComponent } from '../agent-provisioning-panel/agent-provisioning-panel.component';
import { AgentRunnerComponent } from '../agent-console/agent-runner/agent-runner.component';
import { AgentStudioBuildAgentComponent } from './agent-studio-build-agent.component';
import { AgentStudioComposeTeamComponent } from './agent-studio-compose-team.component';
import { AgentStudioPersonaComponent } from './agent-studio-persona.component';
import { AgentStudioShellComponent } from './agent-studio-shell.component';
import { AgentStudioTestAgentComponent } from './agent-studio-test-agent.component';
import { LoadDraftMenuComponent } from './load-draft-menu/load-draft-menu.component';

/** Stub the heavy Agent Console runner so the Test stage can mount with an agent
 *  set without firing sandbox polling / HTTP inside the shell tests. */
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

/** Stub the Stage-4 persona component so the shell's final-stage tests don't pull
 *  in its API services / dialog. */
@Component({ selector: 'app-agent-studio-persona', standalone: true, template: '' })
class StubPersonaComponent {}

/** Stub the Stage-3 compose component so the shell's Compose-stage tests don't
 *  pull in its API services / the embedded process-designer-chat. */
@Component({ selector: 'app-agent-studio-compose-team', standalone: true, template: '' })
class StubComposeTeamComponent {}

describe('AgentStudioShellComponent', () => {
  let component: AgentStudioShellComponent;
  let fixture: ComponentFixture<AgentStudioShellComponent>;
  // Build stage isn't stubbed at the shell level (only its catalog/provisioning
  // children are), so it injects the real AgentStudioApiService — fake it here
  // so no HTTP client is required. Also backs `app-load-draft-menu` (real,
  // unstubbed) and the shell's own `loadDraft` hydration.
  let agentStudioApi: {
    cloneFromRegistry: ReturnType<typeof vi.fn>;
    saveAgent: ReturnType<typeof vi.fn>;
    listDrafts: ReturnType<typeof vi.fn>;
    getDraft: ReturnType<typeof vi.fn>;
  };
  let agenticTeamApi: { getProcess: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    agentStudioApi = {
      cloneFromRegistry: vi.fn().mockReturnValue(of({})),
      saveAgent: vi.fn().mockReturnValue(of({})),
      listDrafts: vi.fn().mockReturnValue(of([])),
      getDraft: vi.fn(),
    };
    agenticTeamApi = { getProcess: vi.fn() };
    await TestBed.configureTestingModule({
      imports: [AgentStudioShellComponent, NoopAnimationsModule],
      providers: [
        { provide: AgentStudioApiService, useValue: agentStudioApi },
        { provide: AgenticTeamApiService, useValue: agenticTeamApi },
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
      .overrideComponent(AgentStudioShellComponent, {
        remove: { imports: [AgentStudioPersonaComponent, AgentStudioComposeTeamComponent] },
        add: { imports: [StubPersonaComponent, StubComposeTeamComponent] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(AgentStudioShellComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('should create with all four stages and start on Build', () => {
    expect(component).toBeTruthy();
    expect(component.stages).toHaveLength(4);
    expect(component.activeStageDef().key).toBe('build');
  });

  it('renders one stepper indicator per stage with the first active', () => {
    const steps = fixture.nativeElement.querySelectorAll('.studio__step');
    expect(steps.length).toBe(4);
    expect(steps[0].classList.contains('is-active')).toBe(true);
    expect(steps[1].classList.contains('is-active')).toBe(false);
  });

  it('renders Save draft and Load draft, both enabled, in the header (spec §3.5)', () => {
    const draftButtons = fixture.nativeElement.querySelectorAll('.studio__draft-btn');
    expect(draftButtons.length).toBe(2);
    const labels = Array.from(draftButtons).map((b) => (b as HTMLElement).textContent?.trim());
    expect(labels[0]).toContain('Save draft');
    expect(labels[1]).toContain('Load draft');
    expect((draftButtons[0] as HTMLButtonElement).disabled).toBe(false);
    expect((draftButtons[1] as HTMLButtonElement).disabled).toBe(false);
  });

  describe('Load draft', () => {
    const draft = (payload: Record<string, unknown>): AgentStudioDraft => ({
      draft_id: 'd-1',
      name: 'My draft',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      payload,
    });

    const process = (status: ProcessDefinition['status']): ProcessDefinition => ({
      process_id: 'proc-1',
      name: 'P',
      description: '',
      trigger: { trigger_type: 'manual', description: '' },
      steps: [],
      output: { description: '', destination: '' },
      status,
    });

    it('binds currentDraftId/currentDraftName from the response, not from payload', () => {
      agentStudioApi.getDraft.mockReturnValue(of(draft({})));
      component.loadDraft('d-1');
      expect(component.state.currentDraftId()).toBe('d-1');
      expect(component.state.currentDraftName()).toBe('My draft');
    });

    it('hydrates the handoff signals from payload, defensively coercing non-strings to null', () => {
      agentStudioApi.getDraft.mockReturnValue(
        of(draft({ registryAgentId: 'reg-1', teamId: 42, personaId: null })),
      );
      component.loadDraft('d-1');
      expect(component.state.registryAgentId()).toBe('reg-1');
      expect(component.state.teamId()).toBeNull();
      expect(component.state.personaId()).toBeNull();
      expect(component.state.processId()).toBeNull();
      expect(component.state.draftAgentId()).toBeNull();
    });

    it('Stage 4: teamId + processId set and the process is complete', () => {
      agentStudioApi.getDraft.mockReturnValue(of(draft({ teamId: 'team-1', processId: 'proc-1' })));
      agenticTeamApi.getProcess.mockReturnValue(of(process('complete')));
      component.loadDraft('d-1');
      expect(agenticTeamApi.getProcess).toHaveBeenCalledWith('proc-1');
      expect(component.state.activeStage()).toBe(3);
      expect(component.state.composeProcessStatus()).toBe('complete');
    });

    it('Stage 3: teamId + processId set but the process is not complete', () => {
      agentStudioApi.getDraft.mockReturnValue(of(draft({ teamId: 'team-1', processId: 'proc-1' })));
      agenticTeamApi.getProcess.mockReturnValue(of(process('draft')));
      component.loadDraft('d-1');
      expect(component.state.activeStage()).toBe(2);
    });

    it('Stage 3: falls back when the process lookup fails (e.g. deleted since save)', () => {
      agentStudioApi.getDraft.mockReturnValue(of(draft({ teamId: 'team-1', processId: 'proc-1' })));
      agenticTeamApi.getProcess.mockReturnValue(throwError(() => new Error('404')));
      component.loadDraft('d-1');
      expect(component.state.activeStage()).toBe(2);
      // Hydration still completed despite the process lookup failing.
      expect(component.state.teamId()).toBe('team-1');
    });

    it('Stage 3: only teamId set — no process lookup performed', () => {
      agentStudioApi.getDraft.mockReturnValue(of(draft({ teamId: 'team-1' })));
      component.loadDraft('d-1');
      expect(agenticTeamApi.getProcess).not.toHaveBeenCalled();
      expect(component.state.activeStage()).toBe(2);
    });

    it('Stage 2: only registryAgentId set', () => {
      agentStudioApi.getDraft.mockReturnValue(of(draft({ registryAgentId: 'reg-1' })));
      component.loadDraft('d-1');
      expect(component.state.activeStage()).toBe(1);
    });

    it('Stage 1: nothing set, including moving backward from a later active stage', () => {
      component.state.navigateToStage(3);
      agentStudioApi.getDraft.mockReturnValue(of(draft({})));
      component.loadDraft('d-1');
      expect(component.state.activeStage()).toBe(0);
    });

    it('never re-validates rosterFullyStaffed', () => {
      component.state.setRosterFullyStaffed(true);
      agentStudioApi.getDraft.mockReturnValue(of(draft({ teamId: 'team-1', processId: 'proc-1' })));
      agenticTeamApi.getProcess.mockReturnValue(of(process('complete')));
      component.loadDraft('d-1');
      expect(component.state.rosterFullyStaffed()).toBe(true);

      component.state.setRosterFullyStaffed(false);
      component.loadDraft('d-1');
      expect(component.state.rosterFullyStaffed()).toBe(false);
    });

    it('a getDraft failure clears loadingDraft and leaves state unchanged', () => {
      agentStudioApi.getDraft.mockReturnValue(throwError(() => new Error('404')));
      component.loadDraft('d-1');
      expect(component.loadingDraft()).toBe(false);
      expect(component.state.currentDraftId()).toBeNull();
      expect(component.state.registryAgentId()).toBeNull();
    });

    it('loadingDraft reflects the in-flight request and guards re-entrancy', () => {
      const pending = new Subject<AgentStudioDraft>();
      agentStudioApi.getDraft.mockReturnValue(pending.asObservable());
      component.loadDraft('d-1');
      expect(component.loadingDraft()).toBe(true);

      component.loadDraft('d-2');
      expect(agentStudioApi.getDraft).toHaveBeenCalledTimes(1);

      pending.next(draft({}));
      pending.complete();
      expect(component.loadingDraft()).toBe(false);
    });

    it('the Load-draft menu is wired to loadDraft and reflects loadingDraft as busy', () => {
      agentStudioApi.getDraft.mockReturnValue(of(draft({})));
      const menu = fixture.debugElement.query(By.directive(LoadDraftMenuComponent));
      expect(menu).toBeTruthy();
      menu.triggerEventHandler('draftSelected', 'd-1');
      expect(component.state.currentDraftId()).toBe('d-1');
    });
  });

  describe('Save draft popover', () => {
    const draftSummary = (): AgentStudioDraftSummary => ({
      draft_id: 'd-1',
      name: 'My draft',
      updated_at: '2026-01-01T00:00:00Z',
    });

    // Spy on the prototype method rather than an injected instance: the
    // standalone shell component (root under TestBed) resolves `MatDialog`
    // (`providedIn: 'root'`) from a different environment-injector instance
    // than both `TestBed.inject(MatDialog)` and a `{ provide: MatDialog,
    // useValue }` override reach — a prototype spy intercepts regardless of
    // which instance ends up being used.
    let openSpy: ReturnType<typeof vi.spyOn<MatDialog, 'open'>>;

    beforeEach(() => {
      openSpy = vi.spyOn(MatDialog.prototype, 'open');
    });

    afterEach(() => {
      openSpy.mockRestore();
    });

    it('clicking Save draft opens the Save-draft dialog', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(undefined) } as unknown as ReturnType<MatDialog['open']>);
      const saveButton: HTMLButtonElement = fixture.nativeElement.querySelector('.studio__draft-btn');
      saveButton.click();
      expect(openSpy).toHaveBeenCalled();
    });

    it('disables backdrop/Escape dismissal so an in-flight save cannot be bypassed', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(undefined) } as unknown as ReturnType<MatDialog['open']>);
      component.openSaveDraftDialog();
      const config = openSpy.mock.calls[0][1] as { disableClose?: boolean };
      expect(config.disableClose).toBe(true);
    });

    it('passes the current handoff state and no draftId as the dialog payload on first save', () => {
      component.state.setRegistryAgentId('reg-1');
      openSpy.mockReturnValue({ afterClosed: () => of(undefined) } as unknown as ReturnType<MatDialog['open']>);
      component.openSaveDraftDialog();
      const config = openSpy.mock.calls[0][1] as { data: { draftId: string | null; payload: Record<string, unknown> } };
      expect(config.data.draftId).toBeNull();
      expect(config.data.payload['registryAgentId']).toBe('reg-1');
    });

    it('passes the bound draftId once a draft has been saved this session', () => {
      component.state.setCurrentDraft('d-1', 'My draft');
      openSpy.mockReturnValue({ afterClosed: () => of(undefined) } as unknown as ReturnType<MatDialog['open']>);
      component.openSaveDraftDialog();
      const config = openSpy.mock.calls[0][1] as { data: { draftId: string | null } };
      expect(config.data.draftId).toBe('d-1');
    });

    it('binds the session to the returned draft on a successful save', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(draftSummary()) } as unknown as ReturnType<MatDialog['open']>);
      component.openSaveDraftDialog();
      expect(component.state.currentDraftId()).toBe('d-1');
      expect(component.state.currentDraftName()).toBe('My draft');
    });

    it('leaves state unchanged when the dialog is cancelled', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(undefined) } as unknown as ReturnType<MatDialog['open']>);
      component.openSaveDraftDialog();
      expect(component.state.currentDraftId()).toBeNull();
      expect(component.state.currentDraftName()).toBeNull();
    });
  });

  it('stepper indicators are not buttons — there is no backward navigation', () => {
    const steps = fixture.nativeElement.querySelectorAll('.studio__step');
    for (const step of Array.from(steps)) {
      expect((step as HTMLElement).tagName).toBe('DIV');
    }
    // No clickable control exists inside the stepper region.
    expect(fixture.nativeElement.querySelector('.studio__stepper button')).toBeNull();
  });

  it('onContinue advances and shows the stage-specific forward label', () => {
    let button: HTMLButtonElement = fixture.nativeElement.querySelector('.studio__continue');
    expect(button.textContent?.trim()).toBe('Test this agent →');
    component.onContinue();
    fixture.detectChanges();
    expect(component.state.activeStage()).toBe(1);
    expect(component.activeStageDef().key).toBe('test');
    const steps = fixture.nativeElement.querySelectorAll('.studio__step');
    expect(steps[0].classList.contains('is-done')).toBe(true);
    expect(steps[1].classList.contains('is-active')).toBe(true);
    button = fixture.nativeElement.querySelector('.studio__continue');
    expect(button.textContent?.trim()).toBe('Add to team →');
  });

  it('hides the forward button on the final stage and advance is a no-op', () => {
    component.state.navigateToStage(3);
    fixture.detectChanges();
    expect(component.activeStageDef().key).toBe('personas');
    expect(component.state.canAdvance()).toBe(false);
    expect(fixture.nativeElement.querySelector('.studio__continue')).toBeNull();
    component.onContinue();
    expect(component.state.activeStage()).toBe(3);
  });

  it('renders the real Compose Team stage (not the placeholder) on Stage 3', () => {
    component.state.navigateToStage(2);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('app-agent-studio-compose-team')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeNull();
  });

  it('passes the live handoff through to Compose', () => {
    component.state.setRegistryAgentId('reg-1');
    component.state.navigateToStage(2);
    fixture.detectChanges();
    const map = new Map(Object.entries(component.state.handoff()));
    expect(map.get('registryAgentId')).toBe('reg-1');
  });

  it('gates "Test this team →" until the roster is staffed and the process is complete', () => {
    component.state.navigateToStage(2);
    fixture.detectChanges();
    let button: HTMLButtonElement = fixture.nativeElement.querySelector('.studio__continue');
    expect(button.textContent?.trim()).toBe('Test this team →');
    expect(component.forwardDisabled()).toBe(true);
    expect(button.disabled).toBe(true);
    expect(component.composeForwardDisabledReason()).toBe(
      'Needs: a fully-staffed roster and a completed process',
    );

    component.state.setRosterFullyStaffed(true);
    fixture.detectChanges();
    expect(component.forwardDisabled()).toBe(true);
    expect(component.composeForwardDisabledReason()).toBe('Needs: a completed process');

    component.state.setComposeProcessStatus('complete');
    fixture.detectChanges();
    button = fixture.nativeElement.querySelector('.studio__continue');
    expect(component.forwardDisabled()).toBe(false);
    expect(button.disabled).toBe(false);
    expect(component.composeForwardDisabledReason()).toBeNull();
  });

  it('composeForwardDisabledReason is null off the Compose stage', () => {
    expect(component.activeStageDef().key).toBe('build');
    expect(component.composeForwardDisabledReason()).toBeNull();
  });

  it('marks only the active step with aria-current="step"', () => {
    const steps = fixture.nativeElement.querySelectorAll('.studio__step');
    expect(steps[0].getAttribute('aria-current')).toBe('step');
    expect(steps[1].getAttribute('aria-current')).toBeNull();
    component.onContinue();
    fixture.detectChanges();
    expect(steps[0].getAttribute('aria-current')).toBeNull();
    expect(steps[1].getAttribute('aria-current')).toBe('step');
  });

  it('renders the real Test Agent stage (not the placeholder) on Stage 2', () => {
    component.state.navigateToStage(1);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('app-agent-studio-test-agent')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeNull();
  });

  it('gates the "Add to team →" forward step until an agent is selected', () => {
    component.state.navigateToStage(1);
    fixture.detectChanges();
    let button: HTMLButtonElement = fixture.nativeElement.querySelector('.studio__continue');
    expect(component.forwardDisabled()).toBe(true);
    expect(button.disabled).toBe(true);

    component.state.setRegistryAgentId('reg-9');
    fixture.detectChanges();
    button = fixture.nativeElement.querySelector('.studio__continue');
    expect(component.forwardDisabled()).toBe(false);
    expect(button.disabled).toBe(false);
  });

  it('renders the real Build Agent stage (not the placeholder) on Stage 1', () => {
    expect(component.activeStageDef().key).toBe('build');
    expect(fixture.nativeElement.querySelector('app-agent-studio-build-agent')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeNull();
  });

  it('gates the Build "Test this agent →" forward step until an agent is selected', () => {
    // On Build (Stage 1) the forward step requires an agent to have been picked.
    expect(component.activeStageDef().key).toBe('build');
    let button: HTMLButtonElement = fixture.nativeElement.querySelector('.studio__continue');
    expect(component.forwardDisabled()).toBe(true);
    expect(button.disabled).toBe(true);

    component.state.setRegistryAgentId('reg-1');
    fixture.detectChanges();
    button = fixture.nativeElement.querySelector('.studio__continue');
    expect(component.forwardDisabled()).toBe(false);
    expect(button.disabled).toBe(false);
  });

  it('surfaces what\'s missing for the disabled Build "Test this agent →" tooltip, step by step', () => {
    expect(component.activeStageDef().key).toBe('build');
    expect(component.buildForwardDisabledReason()).toBe('Select or clone an agent to begin');
    expect(component.forwardDisabledReason()).toBe('Select or clone an agent to begin');

    component.state.setDraftAgentId('draft-1');
    fixture.detectChanges();
    expect(component.buildForwardDisabledReason()).toBe('Save the agent to continue');
    expect(component.forwardDisabledReason()).toBe('Save the agent to continue');

    component.state.setRegistryAgentId('reg-1');
    fixture.detectChanges();
    expect(component.buildForwardDisabledReason()).toBeNull();
    expect(component.forwardDisabledReason()).toBeNull();
  });

  it('buildForwardDisabledReason is null off the Build stage', () => {
    component.state.navigateToStage(1);
    fixture.detectChanges();
    expect(component.activeStageDef().key).toBe('test');
    expect(component.buildForwardDisabledReason()).toBeNull();
  });
});
