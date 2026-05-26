import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { vi, beforeEach } from 'vitest';
import { AccessibilityApiService } from '../../services/accessibility-api.service';
import { AccessibilityDashboardComponent } from './accessibility-dashboard.component';

describe('AccessibilityDashboardComponent (extra coverage)', () => {
  let component: AccessibilityDashboardComponent;
  let fixture: ComponentFixture<AccessibilityDashboardComponent>;
  let api: {
    healthCheck: ReturnType<typeof vi.fn>;
    createAudit: ReturnType<typeof vi.fn>;
    retestFindings: ReturnType<typeof vi.fn>;
    downloadExport: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    api = {
      healthCheck: vi.fn().mockReturnValue(of({ status: 'ok' })),
      createAudit: vi.fn().mockReturnValue(of({ job_id: 'j1', audit_id: 'a1' })),
      retestFindings: vi.fn().mockReturnValue(of({ job_id: 'j2' })),
      downloadExport: vi.fn().mockReturnValue(of(new Blob(['x']))),
    };
    await TestBed.configureTestingModule({
      imports: [AccessibilityDashboardComponent, NoopAnimationsModule],
      providers: [provideHttpClient(), { provide: AccessibilityApiService, useValue: api }],
    }).compileComponents();
    fixture = TestBed.createComponent(AccessibilityDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('checkHealth handles error', () => {
    api.healthCheck.mockReturnValue(throwError(() => ({ message: 'unreachable' })));
    component.checkHealth();
    expect(component.healthError).toBe('unreachable');
    expect(component.healthLoading).toBe(false);
  });

  it('onTabChange maps each tab index', () => {
    const labels: { index: number; tab: string }[] = [
      { index: 0, tab: 'create' },
      { index: 1, tab: 'status' },
      { index: 2, tab: 'findings' },
      { index: 3, tab: 'report' },
      { index: 4, tab: 'design-system' },
      { index: 99, tab: 'create' },
    ];
    for (const { index, tab } of labels) {
      component.onTabChange(index);
      expect(component.activeTab).toBe(tab);
    }
  });

  it('onAuditSubmit handles error gracefully', () => {
    api.createAudit.mockReturnValue(throwError(() => new Error('x')));
    component.onAuditSubmit({ url: 'https://e.com' } as never);
    // No throw; jobId remains unset
    expect(component.jobId).toBeNull();
  });

  it('onAssistantLaunched ignores null job_id', () => {
    component.onAssistantLaunched({ job_id: null, conversation_id: 'c' });
    expect(component.jobId).toBeNull();
  });

  it('onAssistantLaunched sets jobId and switches to status tab', () => {
    component.onAssistantLaunched({ job_id: 'jx', conversation_id: 'c' });
    expect(component.jobId).toBe('jx');
    expect(component.activeTab).toBe('status');
  });

  it('onAuditComplete sets lastStatus and auditId', () => {
    const status = { audit_id: 'a99', status: 'complete' } as never;
    component.onAuditComplete(status);
    expect(component.lastStatus).toBe(status);
    expect(component.auditId).toBe('a99');
  });

  it('onViewReport sets auditId and switches tab', () => {
    component.onViewReport('a-new');
    expect(component.auditId).toBe('a-new');
    expect(component.activeTab).toBe('report');
  });

  it('onViewReport without auditId keeps current id', () => {
    component.auditId = 'orig';
    component.onViewReport();
    expect(component.auditId).toBe('orig');
  });

  it('onRetestRequested no-ops without auditId', () => {
    component.auditId = null;
    component.onRetestRequested(['f1']);
    expect(api.retestFindings).not.toHaveBeenCalled();
  });

  it('onRetestRequested handles error', () => {
    component.auditId = 'a1';
    api.retestFindings.mockReturnValue(throwError(() => ({ error: { detail: 'no' } })));
    component.onRetestRequested(['f1']);
    expect(component.retestError).toBe('no');
    expect(component.retestLoading).toBe(false);
  });

  it('onExportRequested no-ops without auditId', () => {
    component.auditId = null;
    component.onExportRequested('json');
    expect(api.downloadExport).not.toHaveBeenCalled();
  });

  it('onExportRequested downloads blob and triggers anchor click', () => {
    component.auditId = 'a1';
    // jsdom does not implement createObjectURL by default; stub it.
    (window.URL as unknown as { createObjectURL: (b: Blob) => string }).createObjectURL = vi.fn(() => 'blob://x');
    (window.URL as unknown as { revokeObjectURL: (s: string) => void }).revokeObjectURL = vi.fn();
    const clickSpy = vi.fn();
    const createElementSpy = vi.spyOn(document, 'createElement').mockReturnValue({
      click: clickSpy,
      set href(_v: string) { /* noop */ },
      set download(_v: string) { /* noop */ },
    } as unknown as HTMLAnchorElement);
    component.onExportRequested('csv');
    expect(api.downloadExport).toHaveBeenCalledWith('a1', 'csv');
    expect(clickSpy).toHaveBeenCalled();
    createElementSpy.mockRestore();
  });

  it('onExportRequested handles error gracefully', () => {
    component.auditId = 'a1';
    api.downloadExport.mockReturnValue(throwError(() => new Error('x')));
    component.onExportRequested('json');
    // No throw
    expect(component).toBeTruthy();
  });

  it('hasCompletedAudit reflects status', () => {
    expect(component.hasCompletedAudit).toBe(false);
    component.lastStatus = { status: 'complete' } as never;
    expect(component.hasCompletedAudit).toBe(true);
    component.lastStatus = { status: 'running' } as never;
    expect(component.hasCompletedAudit).toBe(false);
  });

  it('ngOnDestroy unsubscribes', () => {
    const healthSub = component['healthSub'];
    expect(healthSub).toBeTruthy();
    const spy = vi.spyOn(healthSub!, 'unsubscribe');
    component.ngOnDestroy();
    expect(spy).toHaveBeenCalled();
  });
});
