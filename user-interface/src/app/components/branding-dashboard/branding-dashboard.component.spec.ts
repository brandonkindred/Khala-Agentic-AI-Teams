import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { BrandingApiService } from '../../services/branding-api.service';
import { BrandingDashboardComponent } from './branding-dashboard.component';

describe('BrandingDashboardComponent', () => {
  let component: BrandingDashboardComponent;
  let fixture: ComponentFixture<BrandingDashboardComponent>;
  let apiSpy: {
    health: ReturnType<typeof vi.fn>;
    listClients: ReturnType<typeof vi.fn>;
    listBrands: ReturnType<typeof vi.fn>;
    createClient: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      listClients: vi.fn().mockReturnValue(of([])),
      listBrands: vi.fn().mockReturnValue(of([])),
      createClient: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [BrandingDashboardComponent, NoopAnimationsModule],
      providers: [{ provide: BrandingApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(BrandingDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('loadClients should call api.listClients', () => {
    apiSpy.listClients.mockReturnValue(of([{ id: 'c1', name: 'Client 1' }]));
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
    const client = { id: 'c1', name: 'Client 1' };
    apiSpy.listBrands.mockReturnValue(of([{ id: 'b1', name: 'Brand 1' }]));
    component.selectClient(client as any);
    expect(component.selectedClient).toEqual(client);
    expect(apiSpy.listBrands).toHaveBeenCalledWith('c1');
    expect(component.brands.length).toBe(1);
  });

  it('createClient should call api.createClient and reload clients on success', () => {
    apiSpy.createClient.mockReturnValue(of({ id: 'c2', name: 'New' }));
    apiSpy.listClients.mockReturnValue(of([{ id: 'c2', name: 'New' }]));
    component.newClientName = 'New Client';
    component.createClient();
    expect(apiSpy.createClient).toHaveBeenCalledWith({ name: 'New Client' });
    expect(component.loading).toBe(false);
  });

  it('healthCheck should call api.health', () => {
    component.healthCheck().subscribe((r) => expect(r).toEqual({ status: 'ok' }));
    expect(apiSpy.health).toHaveBeenCalled();
  });
});
