import { Component, Injector } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { MatDialog } from '@angular/material/dialog';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter, RouterOutlet, type Routes } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { Subject, of, throwError } from 'rxjs';
import { vi } from 'vitest';
import type { AgentStudioDraft, AgentStudioDraftSummary } from '../../../models/agent-studio.model';
import { AgenticTeamApiService } from '../../../services/agentic-team-api.service';
import { AgentStudioFacade } from '../../../services/agent-studio.facade';
import type { ProcessDefinition } from '../../../models/agentic-team.model';
import { AgentStudioShellComponent } from './agent-studio-shell.component';
import { LoadDraftMenuComponent } from './load-draft-menu/load-draft-menu.component';

@Component({ selector: 'app-stub-stage-host', standalone: true, template: '' })
class StubStageHostComponent {}

@Component({ selector: 'app-stub-audit-host', standalone: true, template: '' })
class StubAuditHostComponent {}

@Component({
  selector: 'app-stub-nested-parent',
  standalone: true,
  imports: [RouterOutlet],
  template: '<router-outlet />',
})
class StubNestedParentComponent {}

describe('AgentStudioShellComponent', () => {
  let component: AgentStudioShellComponent;
  let fixture: ComponentFixture<AgentStudioShellComponent>;
  // Stage views live on the child host, not the shell. The shell provides
  // AgentStudioFacade at the component level, so TestBed root providers cannot
  // replace it — override the shell's own provider. Load-draft-menu stays real
  // and consumes this same mock. The leftover getProcess lookup is still a
  // direct AgenticTeamApiService call.
  let facade: {
    loadDraft: ReturnType<typeof vi.fn>;
    listDrafts: ReturnType<typeof vi.fn>;
    selectAgent: ReturnType<typeof vi.fn>;
    saveAgent: ReturnType<typeof vi.fn>;
  };
  let agenticTeamApi: { getProcess: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    facade = {
      loadDraft: vi.fn(),
      listDrafts: vi.fn().mockReturnValue(of([])),
      selectAgent: vi.fn().mockReturnValue(of({})),
      saveAgent: vi.fn().mockReturnValue(of({})),
    };
    agenticTeamApi = { getProcess: vi.fn() };
    await TestBed.configureTestingModule({
      imports: [AgentStudioShellComponent, NoopAnimationsModule],
      providers: [
        { provide: AgenticTeamApiService, useValue: agenticTeamApi },
        provideRouter([]),
      ],
    })
      .overrideComponent(AgentStudioShellComponent, {
        remove: { providers: [AgentStudioFacade] },
        add: { providers: [{ provide: AgentStudioFacade, useValue: facade }] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(AgentStudioShellComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  const compileNestedStudioShell = async (children: Routes): Promise<void> => {
    await TestBed.configureTestingModule({
      imports: [AgentStudioShellComponent, NoopAnimationsModule],
      providers: [
        { provide: AgenticTeamApiService, useValue: agenticTeamApi },
        provideRouter([{ path: '', component: AgentStudioShellComponent, children }]),
      ],
    })
      .overrideComponent(AgentStudioShellComponent, {
        remove: { providers: [AgentStudioFacade] },
        add: { providers: [{ provide: AgentStudioFacade, useValue: facade }] },
      })
      .compileComponents();
  };

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
      facade.loadDraft.mockReturnValue(of(draft({})));
      component.loadDraft('d-1');
      expect(component.state.currentDraftId()).toBe('d-1');
      expect(component.state.currentDraftName()).toBe('My draft');
    });

    it('hydrates the handoff signals from payload, defensively coercing non-strings to null', () => {
      facade.loadDraft.mockReturnValue(
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
      facade.loadDraft.mockReturnValue(of(draft({ teamId: 'team-1', processId: 'proc-1' })));
      agenticTeamApi.getProcess.mockReturnValue(of(process('complete')));
      component.loadDraft('d-1');
      expect(agenticTeamApi.getProcess).toHaveBeenCalledWith('proc-1');
      expect(component.state.activeStage()).toBe(3);
      expect(component.state.composeProcessStatus()).toBe('complete');
    });

    it('Stage 3: teamId + processId set but the process is not complete', () => {
      facade.loadDraft.mockReturnValue(of(draft({ teamId: 'team-1', processId: 'proc-1' })));
      agenticTeamApi.getProcess.mockReturnValue(of(process('draft')));
      component.loadDraft('d-1');
      expect(component.state.activeStage()).toBe(2);
    });

    it('Stage 3: falls back when the process lookup fails (e.g. deleted since save)', () => {
      facade.loadDraft.mockReturnValue(of(draft({ teamId: 'team-1', processId: 'proc-1' })));
      agenticTeamApi.getProcess.mockReturnValue(throwError(() => new Error('404')));
      component.loadDraft('d-1');
      expect(component.state.activeStage()).toBe(2);
      // Hydration still completed despite the process lookup failing.
      expect(component.state.teamId()).toBe('team-1');
    });

    it('Stage 3: only teamId set — no process lookup performed', () => {
      facade.loadDraft.mockReturnValue(of(draft({ teamId: 'team-1' })));
      component.loadDraft('d-1');
      expect(agenticTeamApi.getProcess).not.toHaveBeenCalled();
      expect(component.state.activeStage()).toBe(2);
    });

    it('Stage 2: only registryAgentId set', () => {
      facade.loadDraft.mockReturnValue(of(draft({ registryAgentId: 'reg-1' })));
      component.loadDraft('d-1');
      expect(component.state.activeStage()).toBe(1);
    });

    it('Stage 1: nothing set, including moving backward from a later active stage', () => {
      component.state.navigateToStage(3);
      facade.loadDraft.mockReturnValue(of(draft({})));
      component.loadDraft('d-1');
      expect(component.state.activeStage()).toBe(0);
    });

    it('never re-validates rosterFullyStaffed', () => {
      component.state.setRosterFullyStaffed(true);
      facade.loadDraft.mockReturnValue(of(draft({ teamId: 'team-1', processId: 'proc-1' })));
      agenticTeamApi.getProcess.mockReturnValue(of(process('complete')));
      component.loadDraft('d-1');
      expect(component.state.rosterFullyStaffed()).toBe(true);

      component.state.setRosterFullyStaffed(false);
      component.loadDraft('d-1');
      expect(component.state.rosterFullyStaffed()).toBe(false);
    });

    it('a loadDraft failure clears loadingDraft and leaves state unchanged', () => {
      facade.loadDraft.mockReturnValue(throwError(() => new Error('404')));
      component.loadDraft('d-1');
      expect(component.loadingDraft()).toBe(false);
      expect(component.state.currentDraftId()).toBeNull();
      expect(component.state.registryAgentId()).toBeNull();
    });

    it('clears a persisted persona live-run id on hydrate', () => {
      component.state.setPersonaLiveRunId('run-stale');
      facade.loadDraft.mockReturnValue(of(draft({ registryAgentId: 'reg-1' })));
      component.loadDraft('d-1');
      expect(component.state.personaLiveRunId()).toBeNull();
    });

    it('loadingDraft reflects the in-flight request and guards re-entrancy', () => {
      const pending = new Subject<AgentStudioDraft>();
      facade.loadDraft.mockReturnValue(pending.asObservable());
      component.loadDraft('d-1');
      expect(component.loadingDraft()).toBe(true);

      component.loadDraft('d-2');
      expect(facade.loadDraft).toHaveBeenCalledTimes(1);

      pending.next(draft({}));
      pending.complete();
      expect(component.loadingDraft()).toBe(false);
    });

    it('loadingDraft stays true through the nested process-status check, not just the loadDraft call', () => {
      facade.loadDraft.mockReturnValue(of(draft({ teamId: 'team-1', processId: 'proc-1' })));
      const pendingProcess = new Subject<ProcessDefinition>();
      agenticTeamApi.getProcess.mockReturnValue(pendingProcess.asObservable());
      component.loadDraft('d-1');
      // loadDraft already resolved (synchronous `of`), but the process check is
      // still pending — loadingDraft must not have gone false in between.
      expect(component.loadingDraft()).toBe(true);
      pendingProcess.next(process('complete'));
      pendingProcess.complete();
      expect(component.loadingDraft()).toBe(false);
    });

    it('clears a stale composeProcessStatus when the process lookup fails', () => {
      component.state.setComposeProcessStatus('complete');
      facade.loadDraft.mockReturnValue(of(draft({ teamId: 'team-1', processId: 'proc-1' })));
      agenticTeamApi.getProcess.mockReturnValue(throwError(() => new Error('404')));
      component.loadDraft('d-1');
      expect(component.state.composeProcessStatus()).toBeNull();
    });

    it('resets the Build sub-stepper when resolving to Stage 1 from mid-sub-stepper progress', () => {
      component.state.advanceBuildSubStage();
      component.state.advanceBuildSubStage();
      expect(component.state.activeBuildSubStage()).toBe(2);
      facade.loadDraft.mockReturnValue(of(draft({})));
      component.loadDraft('d-1');
      expect(component.state.activeBuildSubStage()).toBe(0);
      expect(component.state.maxReachedBuildSubStage()).toBe(0);
    });

    it('a superseded loadDraft call discards its late loadDraft response instead of corrupting the newer load', () => {
      const firstDraft = new Subject<AgentStudioDraft>();
      facade.loadDraft.mockReturnValueOnce(firstDraft.asObservable());
      component.loadDraft('d-1'); // in flight, not yet resolved

      facade.loadDraft.mockReturnValueOnce(of(draft({ registryAgentId: 'reg-2' })));
      // Simulate the busy-guard having been bypassed (e.g. a direct call) —
      // force loadingDraft back to false so the second call isn't blocked,
      // to isolate the token guard's own protection from the busy guard's.
      component.loadingDraft.set(false);
      component.loadDraft('d-2'); // supersedes the first, resolves synchronously

      expect(component.state.registryAgentId()).toBe('reg-2');
      firstDraft.next(draft({ registryAgentId: 'reg-1-stale' }));
      firstDraft.complete();
      // The stale first response must not have overwritten the newer load.
      expect(component.state.registryAgentId()).toBe('reg-2');
    });

    it('a superseded loadDraft call discards its late getProcess response', () => {
      facade.loadDraft.mockReturnValueOnce(
        of(draft({ teamId: 'team-1', processId: 'proc-1' })),
      );
      const firstProcess = new Subject<ProcessDefinition>();
      agenticTeamApi.getProcess.mockReturnValueOnce(firstProcess.asObservable());
      component.loadDraft('d-1'); // loadDraft resolves, getProcess left pending

      facade.loadDraft.mockReturnValueOnce(of(draft({ registryAgentId: 'reg-2' })));
      component.loadingDraft.set(false); // bypass the busy-guard, isolate the token guard
      component.loadDraft('d-2'); // resolves synchronously to Stage 2

      expect(component.state.activeStage()).toBe(1);
      firstProcess.next(process('complete'));
      firstProcess.complete();
      // The stale getProcess response must not re-navigate the stepper.
      expect(component.state.activeStage()).toBe(1);
    });

    it('the Load-draft menu is wired to loadDraft and reflects loadingDraft as busy', () => {
      facade.loadDraft.mockReturnValue(of(draft({})));
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

    it('disables backdrop/Escape/browser-navigation dismissal so an in-flight save cannot be bypassed', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(undefined) } as unknown as ReturnType<MatDialog['open']>);
      component.openSaveDraftDialog();
      const config = openSpy.mock.calls[0][1] as {
        disableClose?: boolean;
        closeOnNavigation?: boolean;
        injector?: Injector;
      };
      expect(config.disableClose).toBe(true);
      expect(config.closeOnNavigation).toBe(false);
      // Overlay dialogs otherwise resolve from the root injector, where the
      // session-scoped AgentStudioFacade is not provided.
      expect(config.injector).toBeDefined();
      expect(config.injector!.get(AgentStudioFacade)).toBeTruthy();
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

  it('hides the continue footer on the persona-run child and keeps handoff state', async () => {
    TestBed.resetTestingModule();
    await compileNestedStudioShell([
      { path: '', component: StubStageHostComponent },
      {
        path: 'persona-run/:runId',
        component: StubAuditHostComponent,
        data: { hideStudioFooter: true },
      },
    ]);

    const harness = await RouterTestingHarness.create();
    const shell = await harness.navigateByUrl('/', AgentStudioShellComponent);
    shell.state.setRegistryAgentId('reg-keep');
    harness.detectChanges();
    expect(harness.routeNativeElement?.querySelector('.studio__footer')).toBeTruthy();

    await harness.navigateByUrl('/persona-run/run-1');
    harness.detectChanges();
    expect(harness.routeNativeElement?.querySelector('.studio__footer')).toBeNull();
    expect(harness.routeNativeElement?.querySelector('app-stub-audit-host')).toBeTruthy();
    expect(shell.state.registryAgentId()).toBe('reg-keep');

    await harness.navigateByUrl('/');
    harness.detectChanges();
    expect(harness.routeNativeElement?.querySelector('.studio__footer')).toBeTruthy();
    expect(shell.state.registryAgentId()).toBe('reg-keep');
  });

  it('returns to Stage 4 (Personas) when Back to Agent Studio leaves the persona-run child', async () => {
    // "Back to Agent Studio" in the audit panel targets /agent-studio, which resolves to the
    // shell's default child (stage host). The stepper must still show the stage that was active
    // when View full audit was opened — Personas (Stage 4) — not /persona-testing.
    TestBed.resetTestingModule();
    await compileNestedStudioShell([
      { path: '', component: StubStageHostComponent },
      {
        path: 'persona-run/:runId',
        component: StubAuditHostComponent,
        data: { hideStudioFooter: true },
      },
    ]);

    const harness = await RouterTestingHarness.create();
    const shell = await harness.navigateByUrl('/', AgentStudioShellComponent);
    shell.state.navigateToStage(3);
    harness.detectChanges();
    expect(shell.activeStageDef().key).toBe('personas');

    await harness.navigateByUrl('/persona-run/run-1');
    harness.detectChanges();
    expect(harness.routeNativeElement?.querySelector('app-stub-audit-host')).toBeTruthy();
    expect(harness.routeNativeElement?.querySelector('.studio__footer')).toBeNull();

    // Back to Agent Studio → shell default child.
    await harness.navigateByUrl('/');
    harness.detectChanges();
    expect(harness.routeNativeElement?.querySelector('app-stub-stage-host')).toBeTruthy();
    expect(harness.routeNativeElement?.querySelector('.studio__footer')).toBeTruthy();
    expect(shell.state.activeStage()).toBe(3);
    expect(shell.activeStageDef().key).toBe('personas');
  });

  it('hides the continue footer when hideStudioFooter is on a nested child', async () => {
    TestBed.resetTestingModule();
    await compileNestedStudioShell([
      { path: '', component: StubStageHostComponent },
      {
        path: 'persona-run/:runId',
        component: StubNestedParentComponent,
        children: [
          {
            path: '',
            component: StubAuditHostComponent,
            data: { hideStudioFooter: true },
          },
        ],
      },
    ]);

    const harness = await RouterTestingHarness.create();
    const shell = await harness.navigateByUrl('/', AgentStudioShellComponent);
    shell.state.setRegistryAgentId('reg-deep');
    harness.detectChanges();
    expect(harness.routeNativeElement?.querySelector('.studio__footer')).toBeTruthy();

    await harness.navigateByUrl('/persona-run/run-1');
    harness.detectChanges();
    expect(harness.routeNativeElement?.querySelector('.studio__footer')).toBeNull();
    expect(harness.routeNativeElement?.querySelector('app-stub-audit-host')).toBeTruthy();
    expect(shell.state.registryAgentId()).toBe('reg-deep');
  });

  it('returns to the stage host when a draft is loaded from the persona-run child', async () => {
    TestBed.resetTestingModule();
    await compileNestedStudioShell([
      { path: '', component: StubStageHostComponent },
      {
        path: 'persona-run/:runId',
        component: StubAuditHostComponent,
        data: { hideStudioFooter: true },
      },
    ]);

    facade.loadDraft.mockReturnValue(
      of({
        draft_id: 'd-1',
        name: 'My draft',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        payload: { registryAgentId: 'reg-from-draft' },
      }),
    );

    const harness = await RouterTestingHarness.create();
    const shell = await harness.navigateByUrl('/persona-run/run-1', AgentStudioShellComponent);
    harness.detectChanges();
    expect(harness.routeNativeElement?.querySelector('app-stub-audit-host')).toBeTruthy();

    shell.loadDraft('d-1');
    await harness.fixture.whenStable();
    harness.detectChanges();

    expect(harness.routeNativeElement?.querySelector('app-stub-audit-host')).toBeNull();
    expect(harness.routeNativeElement?.querySelector('app-stub-stage-host')).toBeTruthy();
    expect(harness.routeNativeElement?.querySelector('.studio__footer')).toBeTruthy();
    // The draft payload sets only registryAgentId (no teamId), so resolveFurthestStage's
    // furthest-reachable rule lands on Stage 1 (Test) rather than resetting to Stage 0 (Build).
    expect(shell.state.activeStage()).toBe(1);
    expect(shell.state.registryAgentId()).toBe('reg-from-draft');
  });

  it('stays on the persona-run child when loadDraft fails', async () => {
    TestBed.resetTestingModule();
    await compileNestedStudioShell([
      { path: '', component: StubStageHostComponent },
      {
        path: 'persona-run/:runId',
        component: StubAuditHostComponent,
        data: { hideStudioFooter: true },
      },
    ]);

    facade.loadDraft.mockReturnValue(throwError(() => new Error('404')));

    const harness = await RouterTestingHarness.create();
    const shell = await harness.navigateByUrl('/persona-run/run-1', AgentStudioShellComponent);
    harness.detectChanges();

    shell.loadDraft('d-1');
    await harness.fixture.whenStable();
    harness.detectChanges();

    expect(harness.routeNativeElement?.querySelector('app-stub-audit-host')).toBeTruthy();
    expect(shell.state.registryAgentId()).toBeNull();
    expect(shell.loadingDraft()).toBe(false);
  });
});
