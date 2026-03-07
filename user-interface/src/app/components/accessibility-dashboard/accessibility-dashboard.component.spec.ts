import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { AccessibilityApiService } from '../../services/accessibility-api.service';
import { AccessibilityDashboardComponent } from './accessibility-dashboard.component';

describe('AccessibilityDashboardComponent', () => {
  let component: AccessibilityDashboardComponent;
  let fixture: ComponentFixture<AccessibilityDashboardComponent>;
  let apiSpy: {
    healthCheck: ReturnType<typeof vi.fn>;
    createAudit: ReturnType<typeof vi.fn>;
    retestFindings: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      healthCheck: vi.fn().mockReturnValue(of({ status: 'ok' })),
      createAudit: vi.fn(),
      retestFindings: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [AccessibilityDashboardComponent, NoopAnimationsModule],
      providers: [{ provide: AccessibilityApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(AccessibilityDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should check health on init', () => {
    expect(apiSpy.healthCheck).toHaveBeenCalled();
    expect(component.healthStatus).toEqual({ status: 'ok' });
  });

  it('onTabChange should update selectedTabIndex and activeTab', () => {
    component.onTabChange(2);
    expect(component.selectedTabIndex).toBe(2);
    expect(component.activeTab).toBe('findings');
  });

  it('onAuditSubmit should call createAudit and set jobId and switch to status tab', () => {
    apiSpy.createAudit.mockReturnValue(of({ job_id: 'j1', audit_id: 'a1' } as any));
    component.onAuditSubmit({ url: 'https://example.com' } as any);
    expect(apiSpy.createAudit).toHaveBeenCalledWith({ url: 'https://example.com' });
    expect(component.jobId).toBe('j1');
    expect(component.auditId).toBe('a1');
    expect(component.activeTab).toBe('status');
  });

  it('onStatusChange should set lastStatus and auditId', () => {
    const status = { audit_id: 'a1', status: 'running' } as any;
    component.onStatusChange(status);
    expect(component.lastStatus).toEqual(status);
    expect(component.auditId).toBe('a1');
  });

  it('onViewFindings should switch to findings tab', () => {
    component.onViewFindings('a1');
    expect(component.auditId).toBe('a1');
    expect(component.activeTab).toBe('findings');
  });

  it('onRetestRequested should call retestFindings when auditId is set', () => {
    component.auditId = 'a1';
    apiSpy.retestFindings.mockReturnValue(of({ job_id: 'j2' } as any));
    component.onRetestRequested(['f1']);
    expect(apiSpy.retestFindings).toHaveBeenCalledWith('a1', { finding_ids: ['f1'] });
    expect(component.retestLoading).toBe(false);
  });

  it('startNewAudit should clear state and go to create tab', () => {
    component.jobId = 'j1';
    component.auditId = 'a1';
    component.startNewAudit();
    expect(component.jobId).toBeNull();
    expect(component.auditId).toBeNull();
    expect(component.activeTab).toBe('create');
  });

  it('hasActiveJob returns true when jobId is set', () => {
    component.jobId = 'j1';
    expect(component.hasActiveJob).toBe(true);
    component.jobId = null;
    expect(component.hasActiveJob).toBe(false);
  });
});
