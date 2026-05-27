import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, Subject, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, Router } from '@angular/router';
import { vi, beforeEach, afterEach } from 'vitest';
import { MatSnackBar } from '@angular/material/snack-bar';
import { BrandingApiService, type BrandJobStatus } from '../../services/branding-api.service';
import { BrandActivityService } from '../../services/brand-activity.service';
import { BrandingDashboardComponent } from './branding-dashboard.component';
import type { Brand, Client, BrandingMissionSnapshot } from '../../models';

const workspaceClient: Client = { id: 'w1', name: 'My brands', created_at: '2020-01-01', updated_at: '2020-01-01' };

const makeBrand = (id: string, overrides: Partial<Brand> = {}): Brand => ({
  id,
  name: `Brand ${id}`,
  client_id: 'w1',
  conversation_id: `conv-${id}`,
  mission: {
    company_name: 'Co',
    company_description: 'desc',
    target_audience: 'aud',
    values: [],
    differentiators: [],
    desired_voice: 'clear',
  } as BrandingMissionSnapshot,
  created_at: '2020-01-01',
  updated_at: '2020-01-01',
  ...overrides,
} as Brand);

describe('BrandingDashboardComponent (extra coverage)', () => {
  let api: {
    health: ReturnType<typeof vi.fn>;
    listClients: ReturnType<typeof vi.fn>;
    listBrands: ReturnType<typeof vi.fn>;
    createClient: ReturnType<typeof vi.fn>;
    createBrand: ReturnType<typeof vi.fn>;
    runBrand: ReturnType<typeof vi.fn>;
    getBrand: ReturnType<typeof vi.fn>;
    submitRun: ReturnType<typeof vi.fn>;
    observeJob: ReturnType<typeof vi.fn>;
    listJobs: ReturnType<typeof vi.fn>;
    requestMarketResearch: ReturnType<typeof vi.fn>;
    requestDesignAssets: ReturnType<typeof vi.fn>;
    getConversation: ReturnType<typeof vi.fn>;
    createConversation: ReturnType<typeof vi.fn>;
    updateBrand: ReturnType<typeof vi.fn>;
    sendConversationMessage: ReturnType<typeof vi.fn>;
  };
  let snackBar: { open: ReturnType<typeof vi.fn> };
  let router: { navigate: ReturnType<typeof vi.fn> };
  let component: BrandingDashboardComponent;
  let fixture: ComponentFixture<BrandingDashboardComponent>;

  const buildModule = async (route: unknown) => {
    await TestBed.configureTestingModule({
      imports: [BrandingDashboardComponent, NoopAnimationsModule],
      providers: [
        { provide: BrandingApiService, useValue: api },
        { provide: MatSnackBar, useValue: snackBar },
        { provide: ActivatedRoute, useValue: route },
        { provide: Router, useValue: router },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(BrandingDashboardComponent);
    component = fixture.componentInstance;
  };

  beforeEach(() => {
    snackBar = {
      open: vi.fn().mockReturnValue({ onAction: () => ({ subscribe: vi.fn() }) }),
    };
    router = { navigate: vi.fn().mockResolvedValue(true) };
    api = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      listClients: vi.fn().mockReturnValue(of([workspaceClient])),
      listBrands: vi.fn().mockReturnValue(of([])),
      createClient: vi.fn().mockReturnValue(of(workspaceClient)),
      createBrand: vi.fn().mockReturnValue(of(makeBrand('b-new'))),
      runBrand: vi.fn().mockReturnValue(of({})),
      getBrand: vi.fn().mockReturnValue(of(makeBrand('b1'))),
      submitRun: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'queued' })),
      observeJob: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'completed' } as BrandJobStatus)),
      listJobs: vi.fn().mockReturnValue(of([])),
      requestMarketResearch: vi.fn().mockReturnValue(of({ summary: 'A long market research summary' })),
      requestDesignAssets: vi.fn().mockReturnValue(of({ request_id: 'r1', status: 'queued' })),
      getConversation: vi.fn().mockReturnValue(of({ conversation_id: 'c1', messages: [], mission: null, latest_output: null, suggested_questions: [] })),
      createConversation: vi.fn().mockReturnValue(of({ conversation_id: 'c1', messages: [], mission: null, latest_output: null, suggested_questions: [] })),
      updateBrand: vi.fn().mockReturnValue(of(makeBrand('b1'))),
      sendConversationMessage: vi.fn().mockReturnValue(of({ conversation_id: 'c1', messages: [], mission: { company_name: 'New', company_description: 'd', target_audience: 'a' }, latest_output: null, suggested_questions: [] })),
    };
  });

  afterEach(() => TestBed.resetTestingModule());

  // ---------------------------------------------------------------------
  // onChatStateChange / palette / sync
  // ---------------------------------------------------------------------

  it('onChatStateChange updates state and triggers navigation', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.onChatStateChange({
      conversation_id: 'conv-x',
      mission: null,
      latest_output: null,
    } as never);
    expect(component.activeConversationId).toBe('conv-x');
    expect(router.navigate).toHaveBeenCalled();
  });

  it('onSelectPalette updates conversation mission and selected brand', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    const mission: BrandingMissionSnapshot = { company_name: 'C', company_description: 'd', target_audience: 'a', values: [], differentiators: [], desired_voice: 'v' };
    component.conversationMission = mission;
    component.selectedBrand = makeBrand('b1', { mission });
    component.brands = [component.selectedBrand];
    component.onSelectPalette(2);
    expect(component.conversationMission?.selected_palette_index).toBe(2);
    expect(component.selectedBrand.mission.selected_palette_index).toBe(2);
    expect(component.brands[0].mission.selected_palette_index).toBe(2);
  });

  it('onSelectPalette no-op when no mission or brand', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.conversationMission = null;
    component.selectedBrand = null;
    component.onSelectPalette(1);
    expect(component.conversationMission).toBeNull();
  });

  // ---------------------------------------------------------------------
  // onBrandAutoCreated
  // ---------------------------------------------------------------------

  it('onBrandAutoCreated reloads brands and selects', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedClient = workspaceClient;
    api.listBrands.mockReturnValue(of([makeBrand('auto-1', { name: 'Auto' })]));
    component.onBrandAutoCreated('auto-1');
    expect(api.listBrands).toHaveBeenCalledWith('w1');
    expect(component.selectedBrand?.id).toBe('auto-1');
  });

  it('onBrandAutoCreated no-ops without selected client', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedClient = null;
    component.onBrandAutoCreated('x');
    // listBrands was called once during init; not again
    expect(api.listBrands).toHaveBeenCalledTimes(1);
  });

  // ---------------------------------------------------------------------
  // Save-as-brand modal
  // ---------------------------------------------------------------------

  it('onOpenSaveAsBrand opens dialog', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.onOpenSaveAsBrand();
    expect(component.showSaveAsBrandDialog).toBe(true);
    expect(component.saveToAgencyError).toBeNull();
  });

  it('changing workspace closes the save dialog', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.showSaveAsBrandDialog = true;
    const otherClient = { ...workspaceClient, id: 'w2', name: 'Other' };
    component.onWorkspaceChange(otherClient);
    expect(component.showSaveAsBrandDialog).toBe(false);
  });

  it('changing brand closes the save dialog', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.showSaveAsBrandDialog = true;
    component.onBrandChange(makeBrand('b2'));
    expect(component.showSaveAsBrandDialog).toBe(false);
  });

  it('closeSaveAsBrandDialog resets dialog state', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.showSaveAsBrandDialog = true;
    component.saveToAgencyBrandName = 'N';
    component.closeSaveAsBrandDialog();
    expect(component.showSaveAsBrandDialog).toBe(false);
    expect(component.saveToAgencyBrandName).toBe('');
  });

  it('saveConversationToAgency requires mission and client', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.conversationMission = null;
    component.onOpenSaveAsBrand();
    component.saveConversationToAgency();
    expect(component.saveToAgencyError).toContain('No mission');

    component.conversationMission = { company_name: 'C', company_description: 'd', target_audience: 'a', values: [], differentiators: [], desired_voice: 'v' } as BrandingMissionSnapshot;
    component.onOpenSaveAsBrand();
    component.selectedClient = null;
    component.saveConversationToAgency();
    expect(component.saveToAgencyError).toContain('Workspace');
  });

  it('saveConversationToAgency creates brand and runs it', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.conversationMission = { company_name: 'C', company_description: 'd', target_audience: 'a', values: [], differentiators: [], desired_voice: 'v' } as BrandingMissionSnapshot;
    component.selectedClient = workspaceClient;
    component.onOpenSaveAsBrand();
    api.createBrand.mockReturnValue(of(makeBrand('b-new', { name: 'My Brand' })));
    api.listBrands.mockReturnValue(of([makeBrand('b-new', { name: 'My Brand' })]));
    component.saveConversationToAgency();
    expect(api.createBrand).toHaveBeenCalled();
    expect(api.runBrand).toHaveBeenCalled();
  });

  it('saveConversationToAgency handles run failure gracefully', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.conversationMission = { company_name: 'C', company_description: 'd', target_audience: 'a', values: [], differentiators: [], desired_voice: 'v' } as BrandingMissionSnapshot;
    component.selectedClient = workspaceClient;
    component.onOpenSaveAsBrand();
    api.runBrand.mockReturnValue(throwError(() => ({ message: 'fail' })));
    api.createBrand.mockReturnValue(of(makeBrand('b-new')));
    component.saveConversationToAgency();
    expect(api.runBrand).toHaveBeenCalled();
  });

  it('saveConversationToAgency handles createBrand failure', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.conversationMission = { company_name: 'C', company_description: 'd', target_audience: 'a', values: [], differentiators: [], desired_voice: 'v' } as BrandingMissionSnapshot;
    component.selectedClient = workspaceClient;
    component.onOpenSaveAsBrand();
    api.createBrand.mockReturnValue(throwError(() => ({ error: { detail: 'createbrand-fail' } })));
    component.saveConversationToAgency();
    expect(component.saveToAgencyError).toBe('createbrand-fail');
  });

  it('saveConversationToAgency preserves mission snapshot after workspace change', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    const originalMission = { company_name: 'Original', company_description: 'desc', target_audience: 'audience', values: ['v1'], differentiators: [], desired_voice: 'v' } as BrandingMissionSnapshot;
    component.conversationMission = originalMission;
    component.selectedClient = workspaceClient;
    component.onOpenSaveAsBrand();
    component.conversationMission = { company_name: 'Overwritten', company_description: 'other', target_audience: 'other', values: [], differentiators: [], desired_voice: '' } as BrandingMissionSnapshot;
    api.createBrand.mockReturnValue(of(makeBrand('b-new', { name: 'Original' })));
    api.listBrands.mockReturnValue(of([makeBrand('b-new', { name: 'Original' })]));
    component.saveConversationToAgency();
    expect(api.createBrand).toHaveBeenCalledWith('w1', expect.objectContaining({ company_name: 'Original' }));
  });

  // ---------------------------------------------------------------------
  // ensureWorkspaceClient
  // ---------------------------------------------------------------------

  it('ensureWorkspaceClient creates a client when none exist', async () => {
    api.listClients.mockReturnValueOnce(of([])).mockReturnValueOnce(of([workspaceClient]));
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    expect(api.createClient).toHaveBeenCalled();
    expect(component.clients.length).toBe(1);
  });

  it('ensureWorkspaceClient handles create failure', async () => {
    api.listClients.mockReturnValue(of([]));
    api.createClient.mockReturnValue(throwError(() => ({ message: 'create-fail' })));
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    expect(component.clientLoadError).toBe('create-fail');
  });

  it('ensureWorkspaceClient handles inner listClients failure after create', async () => {
    api.listClients.mockReturnValueOnce(of([])).mockReturnValueOnce(throwError(() => ({ message: 'list2-fail' })));
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    expect(component.clientLoadError).toBe('list2-fail');
  });

  it('ensureWorkspaceClient restores pending workspace id', async () => {
    api.listClients.mockReturnValue(of([workspaceClient, { ...workspaceClient, id: 'w2', name: 'Other' }]));
    await buildModule({
      snapshot: { queryParamMap: { get: (k: string) => (k === 'workspaceId' ? 'w2' : null) } },
    });
    fixture.detectChanges();
    expect(component.selectedClient?.id).toBe('w2');
  });

  // ---------------------------------------------------------------------
  // applyDefaultBrandSelection
  // ---------------------------------------------------------------------

  it('selectClient with brands selects the last brand by default', async () => {
    api.listBrands.mockReturnValue(of([makeBrand('b1'), makeBrand('b2')]));
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    expect(component.selectedBrand?.id).toBe('b2');
  });

  it('selectClient restores pending brand id when present', async () => {
    api.listBrands.mockReturnValue(of([makeBrand('b1'), makeBrand('b2')]));
    await buildModule({
      snapshot: { queryParamMap: { get: (k: string) => (k === 'brandId' ? 'b1' : null) } },
    });
    fixture.detectChanges();
    expect(component.selectedBrand?.id).toBe('b1');
  });

  it('selectClient falls back to last when selectedBrand no longer in list', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedBrand = makeBrand('gone');
    api.listBrands.mockReturnValue(of([makeBrand('b1'), makeBrand('b2')]));
    component.selectClient(workspaceClient);
    expect(component.selectedBrand?.id).toBe('b2');
  });

  it('selectClient handles listBrands error', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    api.listBrands.mockReturnValue(throwError(() => new Error('x')));
    component.selectClient(workspaceClient);
    expect(component.brands).toEqual([]);
  });

  // ---------------------------------------------------------------------
  // openFormTabForNewBrand / startFreshConversation / canCreateBrandFromChat
  // ---------------------------------------------------------------------

  it('openEditPanelForNewBrand clears brand and conversation state, opens panel', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedBrand = makeBrand('b1');
    component.activeConversationId = 'conv-b1';
    component.conversationMission = { company_name: 'C' } as BrandingMissionSnapshot;
    component.openEditPanelForNewBrand();
    expect(component.selectedBrand).toBeNull();
    expect(component.activeConversationId).toBeNull();
    expect(component.conversationMission).toBeNull();
    expect(component.conversationLatestOutput).toBeNull();
    expect(component.editPanelOpen).toBe(true);
    expect(component.showCreateBrand).toBe(true);
  });

  it('canCreateBrandFromChat requires conversation + mission', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    expect(component.canCreateBrandFromChat).toBe(false);
    component.activeConversationId = 'c';
    component.conversationMission = { company_name: 'C', company_description: 'd', target_audience: 'a', values: [], differentiators: [], desired_voice: 'v' } as BrandingMissionSnapshot;
    expect(component.canCreateBrandFromChat).toBe(true);
  });

  it('startFreshConversation clears mission and conversation', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedBrand = makeBrand('b');
    component.activeConversationId = 'c';
    component.conversationMission = {} as BrandingMissionSnapshot;
    component.startFreshConversation();
    expect(component.selectedBrand).toBeNull();
    expect(component.activeConversationId).toBeNull();
    expect(component.conversationMission).toBeNull();
  });

  // ---------------------------------------------------------------------
  // createClient (from selector)
  // ---------------------------------------------------------------------

  it('onAddClientFromSelector triggers createClient', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    api.createClient.mockReturnValue(of(workspaceClient));
    component.onAddClientFromSelector('Foo');
    expect(api.createClient).toHaveBeenCalledWith({ name: 'Foo' });
  });

  it('createClient sets error on failure', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    api.createClient.mockReturnValue(throwError(() => ({ message: 'create-fail' })));
    component.newClientName = 'X';
    component.createClient();
    expect(component.error).toBe('create-fail');
  });

  it('createClient skips empty name', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    api.createClient.mockClear();
    component.newClientName = '   ';
    component.createClient();
    expect(api.createClient).not.toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------
  // createBrand (form)
  // ---------------------------------------------------------------------

  it('createBrand submits form and updates state', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedClient = workspaceClient;
    component.newBrandForm.setValue({
      company_name: 'Acme',
      company_description: 'we make widgets',
      target_audience: 'companies',
      name: 'BrandName',
    });
    api.createBrand.mockReturnValue(of(makeBrand('b-new', { name: 'BrandName' })));
    component.createBrand();
    expect(api.createBrand).toHaveBeenCalled();
    expect(component.selectedBrand?.id).toBe('b-new');
    expect(component.highlightedBrandId).toBe('b-new');
  });

  it('createBrand sets error on failure', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedClient = workspaceClient;
    component.newBrandForm.setValue({
      company_name: 'Acme',
      company_description: 'we make widgets',
      target_audience: 'companies',
      name: '',
    });
    api.createBrand.mockReturnValue(throwError(() => ({ error: { detail: 'create-brand-fail' } })));
    component.createBrand();
    expect(component.error).toBe('create-brand-fail');
  });

  it('createBrand skips when form invalid or no client', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedClient = null;
    component.createBrand();
    expect(api.createBrand).not.toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------
  // isGenerating
  // ---------------------------------------------------------------------

  it('isGenerating returns true when activity store has running activity', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    const store = TestBed.inject(BrandActivityService);
    store.start('run', 'b1');
    store.update(store.snapshot()[0].id, { status: 'running' });
    expect(component.isGenerating('b1')).toBe(true);
    expect(component.isGenerating('other')).toBe(false);
  });

  // ---------------------------------------------------------------------
  // requestDesignAssetsForBrand
  // ---------------------------------------------------------------------

  it('requestDesignAssetsForBrand updates activity on success', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedClient = workspaceClient;
    component.requestDesignAssetsForBrand(makeBrand('b1'));
    expect(api.requestDesignAssets).toHaveBeenCalledWith('w1', 'b1');
    expect(component.brandActionMessage).toContain('Design request');
  });

  it('requestDesignAssetsForBrand handles error', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedClient = workspaceClient;
    api.requestDesignAssets.mockReturnValue(throwError(() => ({ message: 'design-fail' })));
    component.requestDesignAssetsForBrand(makeBrand('b1'));
    const store = TestBed.inject(BrandActivityService);
    const failed = store.snapshot().find((a) => a.kind === 'design' && a.status === 'failed');
    expect(failed?.error).toBe('design-fail');
  });

  it('requestDesignAssetsForBrand no-ops without client', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedClient = null;
    api.requestDesignAssets.mockClear();
    component.requestDesignAssetsForBrand(makeBrand('b1'));
    expect(api.requestDesignAssets).not.toHaveBeenCalled();
  });

  it('requestMarketResearchForBrand error path', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedClient = workspaceClient;
    api.requestMarketResearch.mockReturnValue(throwError(() => ({ message: 'rsrch-fail' })));
    component.requestMarketResearchForBrand(makeBrand('b1'));
  });

  it('runBrand no-ops without client', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedClient = null;
    api.submitRun.mockClear();
    component.runBrand(makeBrand('b1'));
    expect(api.submitRun).not.toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------
  // Activity callbacks
  // ---------------------------------------------------------------------

  it('onActivityOpen selects brand and onActivityDismiss removes chip', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    const store = TestBed.inject(BrandActivityService);
    const brand = makeBrand('b1');
    component.brands = [brand];
    const activity = store.start('run', 'b1');
    component.onActivityOpen(activity);
    expect(component.selectedBrand?.id).toBe('b1');

    component.onActivityDismiss(activity);
    expect(store.snapshot().some((a) => a.id === activity.id)).toBe(false);
  });

  it('onActivityRetry replays the original kind', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedClient = workspaceClient;
    const store = TestBed.inject(BrandActivityService);
    const brand = makeBrand('b1');
    const research = store.start('research', 'b1');
    api.requestMarketResearch.mockClear();
    component.onActivityRetry(brand, research);
    expect(api.requestMarketResearch).toHaveBeenCalled();
    const run = store.start('run', 'b1');
    api.submitRun.mockClear();
    component.onActivityRetry(brand, run);
    expect(api.submitRun).toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------
  // hydrateRunningJobs error
  // ---------------------------------------------------------------------

  it('hydrateRunningJobs handles error silently', async () => {
    api.listBrands.mockReturnValue(of([makeBrand('b1')]));
    api.listJobs.mockReturnValue(throwError(() => new Error('x')));
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    // No exception thrown
    expect(component).toBeTruthy();
  });

  // ---------------------------------------------------------------------
  // formatConversationTime
  // ---------------------------------------------------------------------

  it('formatConversationTime returns a formatted string or fallback', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    expect(component.formatConversationTime('')).toBe('');
    const res = component.formatConversationTime('2020-01-01T00:00:00Z');
    expect(res.length).toBeGreaterThan(0);
  });

  // ---------------------------------------------------------------------
  // Edit panel / skipSave / missionUpdate
  // ---------------------------------------------------------------------

  it('onMissionUpdateFromPanel patches conversationMission and calls updateBrand', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedClient = workspaceClient;
    component.selectedBrand = makeBrand('b1');
    component.brands = [component.selectedBrand];
    component.activeConversationId = 'conv-b1';
    component.conversationMission = { company_name: 'Old', company_description: 'd', target_audience: 'a' } as BrandingMissionSnapshot;
    component.editPanelOpen = true;
    const updatedMission = { ...component.selectedBrand.mission, company_name: 'New' };
    api.updateBrand.mockReturnValue(of(makeBrand('b1', { mission: updatedMission })));

    component.onMissionUpdateFromPanel({ company_name: 'New' });

    expect(component.conversationMission?.company_name).toBe('New');
    expect(component.selectedBrand?.mission.company_name).toBe('New');
    expect(api.updateBrand).toHaveBeenCalled();
    expect(api.sendConversationMessage).toHaveBeenCalledWith('conv-b1', expect.stringContaining('New'), false);
    expect(component.editPanelOpen).toBe(false);
  });

  it('onMissionUpdateFromPanel works without existing mission', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.conversationMission = null;
    component.selectedBrand = null;
    component.onMissionUpdateFromPanel({ company_name: 'X' });
    expect(component.conversationMission?.company_name).toBe('X');
  });

  it('onMissionUpdateFromPanel syncs edits to conversation when no brand is selected', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedBrand = null;
    component.selectedClient = workspaceClient;
    component.activeConversationId = 'conv-orphan';
    component.conversationMission = { company_name: 'Old', company_description: 'd', target_audience: 'a' } as BrandingMissionSnapshot;

    component.onMissionUpdateFromPanel({ company_name: 'New' });

    expect(api.sendConversationMessage).toHaveBeenCalledWith('conv-orphan', expect.stringContaining('New'), false);
    expect(component.conversationMission?.company_name).toBe('New');
  });

  it('onMissionUpdateFromPanel reconciles auto-created brand from conversation response', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedBrand = null;
    component.selectedClient = workspaceClient;
    component.activeConversationId = 'conv-orphan';
    component.conversationMission = { company_name: 'Old', company_description: 'd', target_audience: 'a' } as BrandingMissionSnapshot;
    api.sendConversationMessage.mockReturnValue(of({
      conversation_id: 'conv-orphan',
      brand_id: 'auto-b1',
      messages: [],
      mission: { company_name: 'New', company_description: 'd', target_audience: 'a' },
      latest_output: null,
      suggested_questions: [],
    }));
    api.listBrands.mockReturnValue(of([makeBrand('auto-b1', { name: 'Auto' })]));

    component.onMissionUpdateFromPanel({ company_name: 'New' });

    expect(api.listBrands).toHaveBeenCalledWith('w1');
    expect(component.selectedBrand?.id).toBe('auto-b1');
  });

  it('onMissionUpdateFromPanel handles updateBrand error without crashing', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedClient = workspaceClient;
    const brand = makeBrand('b1');
    component.selectedBrand = brand;
    component.brands = [brand];
    component.conversationMission = { company_name: 'Old', company_description: 'd', target_audience: 'a' } as BrandingMissionSnapshot;
    api.updateBrand.mockReturnValue(throwError(() => ({ message: 'update-fail' })));
    component.onMissionUpdateFromPanel({ company_name: 'Y' });
    expect(api.updateBrand).toHaveBeenCalledWith('w1', 'b1', expect.objectContaining({ company_name: 'Y' }));
    expect(component.conversationMission?.company_name).toBe('Y');
    expect(component.editPanelOpen).toBe(false);
  });

  it('onMissionUpdateFromPanel creates conversation when none exists yet', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedBrand = null;
    component.selectedClient = workspaceClient;
    component.activeConversationId = null;
    component.conversationMission = null;
    api.createConversation.mockReturnValue(of({
      conversation_id: 'conv-new',
      brand_id: null,
      messages: [],
      mission: { company_name: 'Fresh', company_description: 'd', target_audience: 'a' },
      latest_output: null,
      suggested_questions: [],
    }));

    component.onMissionUpdateFromPanel({ company_name: 'Fresh' });

    expect(api.createConversation).toHaveBeenCalledWith(expect.stringContaining('Fresh'), false);
    expect(component.activeConversationId).toBe('conv-new');
  });

  it('openEditPanelForNewBrand syncs query params to clear stale URL', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedBrand = makeBrand('b1');
    component.activeConversationId = 'conv-b1';
    router.navigate.mockClear();

    component.openEditPanelForNewBrand();

    expect(router.navigate).toHaveBeenCalled();
    const lastCall = router.navigate.mock.calls[router.navigate.mock.calls.length - 1];
    expect(lastCall[1].queryParams.brandId).toBeUndefined();
    expect(lastCall[1].queryParams.conversationId).toBeUndefined();
  });

  it('onSkipSaveChange resets brand and conversation when skip is true', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedBrand = makeBrand('b1');
    component.activeConversationId = 'conv-1';
    component.conversationMission = { company_name: 'C' } as BrandingMissionSnapshot;

    component.onSkipSaveChange(true);

    expect(component.skipSave).toBe(true);
    expect(component.selectedBrand).toBeNull();
    expect(component.activeConversationId).toBeNull();
    expect(component.conversationMission).toBeNull();
  });

  it('onSkipSaveChange with false does not reset state', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    component.selectedBrand = makeBrand('b1');
    component.onSkipSaveChange(false);
    expect(component.skipSave).toBe(false);
    expect(component.selectedBrand).not.toBeNull();
  });

  it('toggleEditPanel toggles editPanelOpen', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    expect(component.editPanelOpen).toBe(false);
    component.toggleEditPanel();
    expect(component.editPanelOpen).toBe(true);
    component.toggleEditPanel();
    expect(component.editPanelOpen).toBe(false);
  });

  // ---------------------------------------------------------------------
  // Lifecycle
  // ---------------------------------------------------------------------

  it('ngOnDestroy clears subscriptions and activity polls', async () => {
    await buildModule({ snapshot: { queryParamMap: { get: () => null } } });
    fixture.detectChanges();
    const sub = new Subject<void>().subscribe(vi.fn());
    component['activityPolls'].set('a', sub);
    const spy = vi.spyOn(sub, 'unsubscribe');
    component.ngOnDestroy();
    expect(spy).toHaveBeenCalled();
    expect(component['activityPolls'].size).toBe(0);
  });
});
