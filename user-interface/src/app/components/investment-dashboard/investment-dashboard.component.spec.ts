import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { InvestmentDashboardComponent } from './investment-dashboard.component';

describe('InvestmentDashboardComponent', () => {
  let component: InvestmentDashboardComponent;
  let fixture: ComponentFixture<InvestmentDashboardComponent>;
  let apiSpy: { healthCheck: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = { healthCheck: vi.fn().mockReturnValue(of({ status: 'ok' })) };
    await TestBed.configureTestingModule({
      imports: [InvestmentDashboardComponent, NoopAnimationsModule],
      providers: [{ provide: InvestmentApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(InvestmentDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should check health on init', () => {
    expect(apiSpy.healthCheck).toHaveBeenCalled();
    expect(component.healthStatus).toBe('healthy');
  });

  it('should set unhealthy on health check failure', () => {
    apiSpy.healthCheck.mockReturnValue(throwError(() => new Error('fail')));
    component.checkHealth();
    expect(component.healthStatus).toBe('unhealthy');
  });

  it('onProfileCreated should set currentIPS and switch tab', () => {
    const ips = { profile_id: 'p1' } as any;
    component.onProfileCreated(ips);
    expect(component.currentIPS).toEqual(ips);
    expect(component.showProfileForm).toBe(false);
    expect(component.selectedTabIndex).toBe(1);
  });

  it('onProfileFormCancelled should hide form', () => {
    component.showProfileForm = true;
    component.onProfileFormCancelled();
    expect(component.showProfileForm).toBe(false);
  });
});
