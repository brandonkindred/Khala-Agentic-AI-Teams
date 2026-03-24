import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute } from '@angular/router';
import { vi } from 'vitest';
import { MatSnackBar } from '@angular/material/snack-bar';
import { BrandingApiService } from '../../services/branding-api.service';
import { BrandingDashboardComponent } from './branding-dashboard.component';

const fakeRoute = { snapshot: { queryParamMap: { get: () => null } } };

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
  };
  let snackBarSpy: { open: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    snackBarSpy = {
      open: vi.fn().mockReturnValue({
        onAction: () => ({ subscribe: vi.fn() }),
      }),
    };
    const emptyConversationState = { conversation_id: 'c1', messages: [], mission: null, latest_output: null, suggested_questions: [] };
    apiSpy = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      listClients: vi.fn().mockReturnValue(of([workspaceClient])),
      listBrands: vi.fn().mockReturnValue(of([])),
      createClient: vi.fn(),
      createBrand: vi.fn(),
      getBrand: vi.fn(),
      createConversation: vi.fn().mockReturnValue(of(emptyConversationState)),
    };
    await TestBed.configureTestingModule({
      imports: [BrandingDashboardComponent, NoopAnimationsModule],
      providers: [
        { provide: BrandingApiService, useValue: apiSpy },
        { provide: MatSnackBar, useValue: snackBarSpy },
        { provide: ActivatedRoute, useValue: fakeRoute },
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
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(BrandingDashboardComponent);
    fixture.detectChanges();
    await fixture.whenStable();

    expect(api.createClient).toHaveBeenCalledWith({ name: 'My brands' });
    expect(fixture.componentInstance.selectedClient?.id).toBe('w1');
  });
});
