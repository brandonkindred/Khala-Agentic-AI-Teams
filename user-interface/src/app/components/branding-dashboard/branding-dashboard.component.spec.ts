import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Subject, of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, Router } from '@angular/router';
import { vi } from 'vitest';
import { MatSnackBar } from '@angular/material/snack-bar';
import { BrandingApiService, type BrandJobStatus } from '../../services/branding-api.service';
import { BrandActivityService } from '../../services/brand-activity.service';
import { BrandingDashboardComponent } from './branding-dashboard.component';
import type { Brand } from '../../models';

const fakeRoute = { snapshot: { queryParamMap: { get: () => null } } };
const fakeRouteWith = (params: Record<string, string | null>) => ({
  snapshot: { queryParamMap: { get: (k: string) => params[k] ?? null } },
});

const workspaceClient = { id: 'w1', name: 'My brands', created_at: '2020-01-01', updated_at: '2020-01-01' };

describe('BrandingDashboardComponent', () => {
  let component: BrandingDashboardComponent;
  let fixture: ComponentFixture<BrandingDashboardComponent>;
  let apiSpy: {
    health: ReturnType<typeof vi.fn>;
    listClients: ReturnType<typeof vi.fn>;
    listBrands: ReturnType<typeof vi.fn>;
    createClient: ReturnType<typeof vi.fn>;
    createBrand: ReturnType<typeof vi.fn>;
    getBrand: ReturnType<typeof vi.fn>;
    createConversation: ReturnType<typeof vi.fn>;
    submitRun: ReturnType<typeof vi.fn>;
    observeJob: ReturnType<typeof vi.fn>;
    listJobs: ReturnType<typeof vi.fn>;
    requestMarketResearch: ReturnType<typeof vi.fn>;
    requestDesignAssets: ReturnType<typeof vi.fn>;
  };
  let snackBarSpy: { open: ReturnType<typeof vi.fn> };
  let routerSpy: { navigate: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    snackBarSpy = {
      open: vi.fn().mockReturnValue({
        onAction: () => ({ subscribe: vi.fn() }),
      }),
    };
    routerSpy = { navigate: vi.fn().mockResolvedValue(true) };
    const emptyConversationState = { conversation_id: 'c1', messages: [], mission: null, latest_output: null, suggested_questions: [] };
    apiSpy = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      listClients: vi.fn().mockReturnValue(of([workspaceClient])),
      listBrands: vi.fn().mockReturnValue(of([])),
      createClient: vi.fn(),
      createBrand: vi.fn(),
      getBrand: vi.fn(),
      createConversation: vi.fn().mockReturnValue(of(emptyConversationState)),
      submitRun: vi.fn(),
      observeJob: vi.fn(),
      listJobs: vi.fn().mockReturnValue(of([])),
      requestMarketResearch: vi.fn(),
      requestDesignAssets: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [BrandingDashboardComponent, NoopAnimationsModule],
      providers: [
        { provide: BrandingApiService, useValue: apiSpy },
        { provide: MatSnackBar, useValue: snackBarSpy },
        { provide: ActivatedRoute, useValue: fakeRoute },
        { provide: Router, useValue: routerSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(BrandingDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should expose selectedTabIndex for two-way tab binding (not forced to 0)', () => {
    expect(typeof component.selectedTabIndex).toBe('number');
    component.selectedTabIndex = 1;
    fixture.detectChanges();
    expect(component.selectedTabIndex).toBe(1);
  });

  it('loadClients should call api.listClients', () => {
    apiSpy.listClients.mockReturnValue(of([{ id: 'c1', name: 'Client 1', created_at: '', updated_at: '' }]));
    component.loadClients();
    expect(apiSpy.listClients).toHaveBeenCalled();
    expect(component.clients.length).toBe(1);
  });

  it('loadClients should set clientLoadError on failure', () => {
    apiSpy.listClients.mockReturnValue(throwError(() => ({ error: { detail: 'Error' } })));
    component.loadClients();
    expect(component.clientLoadError).toBeTruthy();
  });

  it('selectClient should set selectedClient and call listBrands', () => {
    const client = { id: 'c1', name: 'Client 1', created_at: '', updated_at: '' };
    apiSpy.listBrands.mockReturnValue(of([{ id: 'b1', name: 'Brand 1', client_id: 'c1', status: 'draft', mission: {} as any, version: 1, history: [], created_at: '', updated_at: '' }]));
    component.selectClient(client as any);
    expect(component.selectedClient).toEqual(client);
    expect(apiSpy.listBrands).toHaveBeenCalledWith('c1');
    expect(component.brands.length).toBe(1);
  });

  it('createClient should call api.createClient and reload clients on success', () => {
    apiSpy.createClient.mockReturnValue(of({ id: 'c2', name: 'New', created_at: '', updated_at: '' }));
    apiSpy.listClients.mockReturnValue(of([{ id: 'c2', name: 'New', created_at: '', updated_at: '' }]));
    component.newClientName = 'New workspace';
    component.createClient();
    expect(apiSpy.createClient).toHaveBeenCalledWith({ name: 'New workspace' });
    expect(component.brandFormBusy).toBe(false);
  });

  it('healthCheck should call api.health', () => {
    component.healthCheck().subscribe((r) => expect(r).toEqual({ status: 'ok' }));
    expect(apiSpy.health).toHaveBeenCalled();
  });

  it('selectBrandForChat should set activeConversationId from brand', () => {
    const brand = {
      id: 'b1',
      client_id: 'w1',
      name: 'B',
      status: 'draft' as const,
      conversation_id: 'conv-123',
      mission: {} as any,
      version: 1,
      history: [],
      created_at: '',
      updated_at: '',
    };
    component.selectBrandForChat(brand);
    expect(component.selectedBrand).toEqual(brand);
    expect(component.activeConversationId).toBe('conv-123');
  });

  it('runBrand submits, tracks the job, and updates the activity chip', () => {
    const brand: Brand = {
      id: 'b1',
      client_id: 'w1',
      name: 'B',
      status: 'draft',
      conversation_id: null,
      mission: {} as any,
      version: 1,
      history: [],
      created_at: '',
      updated_at: '',
    };
    component.selectedClient = { id: 'w1', name: 'My brands', created_at: '', updated_at: '' };
    component.brands = [brand];
    const statusSubject = new Subject<BrandJobStatus>();
    apiSpy.submitRun.mockReturnValue(of({ job_id: 'j1', status: 'queued' }));
    apiSpy.observeJob.mockReturnValue(statusSubject.asObservable());
    apiSpy.getBrand.mockReturnValue(of(brand));

    component.runBrand(brand);

    const store = TestBed.inject(BrandActivityService);
    const running = store.snapshot().find((a) => a.brandId === 'b1');
    expect(running).toBeTruthy();
    expect(running!.jobId).toBe('j1');
    expect(component.isGenerating('b1')).toBe(true);

    statusSubject.next({ job_id: 'j1', status: 'running', current_phase: 'Visual Identity' });
    const mid = store.snapshot().find((a) => a.id === running!.id);
    expect(mid!.status).toBe('running');
    expect(mid!.phase).toBe('Visual Identity');

    statusSubject.next({ job_id: 'j1', status: 'completed', updated_at: '2026-04-22T10:00:00Z' });
    statusSubject.complete();
    const final = store.snapshot().find((a) => a.id === running!.id);
    expect(final!.status).toBe('completed');
    expect(component.isGenerating('b1')).toBe(false);
    expect(apiSpy.getBrand).toHaveBeenCalledWith('w1', 'b1');
  });

  it('runBrand failure pushes a failed activity and exposes the error', () => {
    const brand: Brand = {
      id: 'b1', client_id: 'w1', name: 'B', status: 'draft',
      mission: {} as any, version: 1, history: [], created_at: '', updated_at: '',
    };
    component.selectedClient = { id: 'w1', name: 'My brands', created_at: '', updated_at: '' };
    component.brands = [brand];
    apiSpy.submitRun.mockReturnValue(throwError(() => ({ error: { detail: 'nope' } })));

    component.runBrand(brand);

    const store = TestBed.inject(BrandActivityService);
    const activity = store.snapshot().find((a) => a.brandId === 'b1');
    expect(activity!.status).toBe('failed');
    expect(activity!.error).toBe('nope');
  });

  it('requestMarketResearchForBrand pushes a research activity', () => {
    const brand: Brand = {
      id: 'b1', client_id: 'w1', name: 'B', status: 'draft',
      mission: {} as any, version: 1, history: [], created_at: '', updated_at: '',
    };
    component.selectedClient = { id: 'w1', name: 'My brands', created_at: '', updated_at: '' };
    apiSpy.requestMarketResearch.mockReturnValue(
      of({ summary: 'S', similar_brands: [], insights: [], source: 'x' })
    );

    component.requestMarketResearchForBrand(brand);

    const store = TestBed.inject(BrandActivityService);
    const activity = store.snapshot().find((a) => a.brandId === 'b1');
    expect(activity!.kind).toBe('research');
    expect(activity!.status).toBe('completed');
  });

  it('onActivityRetry removes the old chip and re-fires the same kind', () => {
    const brand: Brand = {
      id: 'b1', client_id: 'w1', name: 'B', status: 'draft',
      mission: {} as any, version: 1, history: [], created_at: '', updated_at: '',
    };
    component.selectedClient = { id: 'w1', name: 'My brands', created_at: '', updated_at: '' };
    apiSpy.requestDesignAssets.mockReturnValue(of({ request_id: 'r1', status: 'pending', artifacts: [] }));
    const store = TestBed.inject(BrandActivityService);
    const failed = store.start('design', 'b1');
    store.update(failed.id, { status: 'failed', error: 'prev failure' });

    component.onActivityRetry(brand, store.snapshot()[0]);

    expect(apiSpy.requestDesignAssets).toHaveBeenCalledWith('w1', 'b1');
    const snap = store.snapshot();
    expect(snap).toHaveLength(1);
    expect(snap[0].id).not.toBe(failed.id);
    expect(snap[0].status).toBe('completed');
  });

  it('selectClient hydrates running jobs for the workspace', () => {
    const brand: Brand = {
      id: 'b1', client_id: 'w1', name: 'B', status: 'draft',
      mission: {} as any, version: 1, history: [], created_at: '', updated_at: '',
    };
    apiSpy.listBrands.mockReturnValue(of([brand]));
    apiSpy.listJobs.mockReturnValue(
      of([{ job_id: 'hydrated', status: 'running', brand_id: 'b1' }])
    );
    apiSpy.observeJob.mockReturnValue(new Subject().asObservable());

    component.selectClient({ id: 'w1', name: 'My brands', created_at: '', updated_at: '' });

    expect(apiSpy.listJobs).toHaveBeenCalledWith(true);
    const store = TestBed.inject(BrandActivityService);
    const hydrated = store.snapshot().find((a) => a.jobId === 'hydrated');
    expect(hydrated).toBeTruthy();
    expect(hydrated!.brandId).toBe('b1');
  });

  it('selectClient writes workspaceId to the URL via syncQueryParams', () => {
    const client = { id: 'w1', name: 'My brands', created_at: '', updated_at: '' };
    apiSpy.listBrands.mockReturnValue(of([]));
    routerSpy.navigate.mockClear();

    component.selectClient(client);

    expect(routerSpy.navigate).toHaveBeenCalled();
    const lastCall = routerSpy.navigate.mock.calls[routerSpy.navigate.mock.calls.length - 1];
    expect(lastCall[1].queryParams.workspaceId).toBe('w1');
  });

  it('onWorkspaceChange delegates to selectClient', () => {
    const client = { id: 'w2', name: 'Other', created_at: '', updated_at: '' };
    apiSpy.listBrands.mockReturnValue(of([]));

    component.onWorkspaceChange(client);

    expect(component.selectedClient).toEqual(client);
    expect(apiSpy.listBrands).toHaveBeenCalledWith('w2');
  });

  it('onBrandChange resumes the brand and sets activeConversationId', () => {
    const brand: Brand = {
      id: 'b1', client_id: 'w1', name: 'B', status: 'draft',
      conversation_id: 'conv-7', mission: {} as any, version: 1, history: [],
      created_at: '', updated_at: '',
    };

    component.onBrandChange(brand);

    expect(component.selectedBrand).toEqual(brand);
    expect(component.activeConversationId).toBe('conv-7');
  });

  it('onAddClientFromSelector creates the workspace via createClient', () => {
    apiSpy.createClient.mockReturnValue(of({ id: 'wN', name: 'New', created_at: '', updated_at: '' }));
    apiSpy.listClients.mockReturnValue(of([{ id: 'wN', name: 'New', created_at: '', updated_at: '' }]));

    component.onAddClientFromSelector('New');

    expect(apiSpy.createClient).toHaveBeenCalledWith({ name: 'New' });
  });
});

describe('BrandingDashboardComponent query-param restore', () => {
  it('restores selected workspace from ?workspaceId on init', async () => {
    const w1 = { id: 'w1', name: 'WS1', created_at: '', updated_at: '' };
    const w2 = { id: 'w2', name: 'WS2', created_at: '', updated_at: '' };
    const snackBar = { open: vi.fn().mockReturnValue({ onAction: () => ({ subscribe: vi.fn() }) }) };
    const router = { navigate: vi.fn().mockResolvedValue(true) };
    const api = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      listClients: vi.fn().mockReturnValue(of([w1, w2])),
      listBrands: vi.fn().mockReturnValue(of([])),
      createClient: vi.fn(),
      createBrand: vi.fn(),
      getBrand: vi.fn(),
      createConversation: vi.fn().mockReturnValue(of({ conversation_id: 'c1', messages: [], mission: null, latest_output: null, suggested_questions: [] })),
      submitRun: vi.fn(),
      observeJob: vi.fn(),
      listJobs: vi.fn().mockReturnValue(of([])),
      requestMarketResearch: vi.fn(),
      requestDesignAssets: vi.fn(),
    };

    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [BrandingDashboardComponent, NoopAnimationsModule],
      providers: [
        { provide: BrandingApiService, useValue: api },
        { provide: MatSnackBar, useValue: snackBar },
        { provide: ActivatedRoute, useValue: fakeRouteWith({ workspaceId: 'w2' }) },
        { provide: Router, useValue: router },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(BrandingDashboardComponent);
    fixture.detectChanges();
    await fixture.whenStable();

    expect(fixture.componentInstance.selectedClient?.id).toBe('w2');
  });

  it('restores selected brand from ?brandId on init', async () => {
    const w1 = { id: 'w1', name: 'WS1', created_at: '', updated_at: '' };
    const b1: Brand = {
      id: 'b1', client_id: 'w1', name: 'B1', status: 'draft',
      mission: {} as any, version: 1, history: [], created_at: '', updated_at: '',
    };
    const b2: Brand = {
      id: 'b2', client_id: 'w1', name: 'B2', status: 'draft',
      mission: {} as any, version: 2, history: [], created_at: '', updated_at: '',
    };
    const snackBar = { open: vi.fn().mockReturnValue({ onAction: () => ({ subscribe: vi.fn() }) }) };
    const router = { navigate: vi.fn().mockResolvedValue(true) };
    const api = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      listClients: vi.fn().mockReturnValue(of([w1])),
      listBrands: vi.fn().mockReturnValue(of([b1, b2])),
      createClient: vi.fn(),
      createBrand: vi.fn(),
      getBrand: vi.fn(),
      createConversation: vi.fn().mockReturnValue(of({ conversation_id: 'c1', messages: [], mission: null, latest_output: null, suggested_questions: [] })),
      submitRun: vi.fn(),
      observeJob: vi.fn(),
      listJobs: vi.fn().mockReturnValue(of([])),
      requestMarketResearch: vi.fn(),
      requestDesignAssets: vi.fn(),
    };

    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [BrandingDashboardComponent, NoopAnimationsModule],
      providers: [
        { provide: BrandingApiService, useValue: api },
        { provide: MatSnackBar, useValue: snackBar },
        { provide: ActivatedRoute, useValue: fakeRouteWith({ brandId: 'b1' }) },
        { provide: Router, useValue: router },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(BrandingDashboardComponent);
    fixture.detectChanges();
    await fixture.whenStable();

    expect(fixture.componentInstance.selectedBrand?.id).toBe('b1');
  });
});

describe('BrandingDashboardComponent workspace bootstrap', () => {
  it('creates default client when API returns no workspaces', async () => {
    const snackBar = { open: vi.fn().mockReturnValue({ onAction: () => ({ subscribe: vi.fn() }) }) };
    let listCalls = 0;
    const emptyConversationState2 = { conversation_id: 'c1', messages: [], mission: null, latest_output: null, suggested_questions: [] };
    const api = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      listClients: vi.fn().mockImplementation(() => {
        listCalls += 1;
        return listCalls === 1 ? of([]) : of([workspaceClient]);
      }),
      listBrands: vi.fn().mockReturnValue(of([])),
      createClient: vi.fn().mockReturnValue(of(workspaceClient)),
      createBrand: vi.fn(),
      getBrand: vi.fn(),
      createConversation: vi.fn().mockReturnValue(of(emptyConversationState2)),
    };

    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [BrandingDashboardComponent, NoopAnimationsModule],
      providers: [
        { provide: BrandingApiService, useValue: api },
        { provide: MatSnackBar, useValue: snackBar },
        { provide: ActivatedRoute, useValue: fakeRoute },
        { provide: Router, useValue: { navigate: vi.fn().mockResolvedValue(true) } },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(BrandingDashboardComponent);
    fixture.detectChanges();
    await fixture.whenStable();

    expect(api.createClient).toHaveBeenCalledWith({ name: 'My brands' });
    expect(fixture.componentInstance.selectedClient?.id).toBe('w1');
  });
});
