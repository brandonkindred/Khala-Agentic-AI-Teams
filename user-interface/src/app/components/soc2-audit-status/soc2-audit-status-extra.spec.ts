import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi, beforeEach, afterEach } from 'vitest';
import { Soc2ComplianceApiService } from '../../services/soc2-compliance-api.service';
import { Soc2AuditStatusComponent } from './soc2-audit-status.component';

describe('Soc2AuditStatusComponent (extra coverage)', () => {
  let api: { getStatus: ReturnType<typeof vi.fn> };
  let component: Soc2AuditStatusComponent;
  let fixture: ComponentFixture<Soc2AuditStatusComponent>;

  beforeEach(async () => {
    api = { getStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running' })) };
    await TestBed.configureTestingModule({
      imports: [Soc2AuditStatusComponent, NoopAnimationsModule],
      providers: [{ provide: Soc2ComplianceApiService, useValue: api }],
    }).compileComponents();
    fixture = TestBed.createComponent(Soc2AuditStatusComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => {
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  it('does not poll without jobId', () => {
    component.jobId = null;
    fixture.detectChanges();
    expect(component.loading).toBe(false);
    expect(api.getStatus).not.toHaveBeenCalled();
  });

  it('polls and stops on terminal status', async () => {
    vi.useFakeTimers();
    api.getStatus.mockReturnValue(of({ job_id: 'j1', status: 'completed' }));
    component.jobId = 'j1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component.status?.status).toBe('completed');
    expect(component['sub']).toBeNull();
  });

  it('stops on failed and cancelled', async () => {
    vi.useFakeTimers();
    api.getStatus.mockReturnValue(of({ job_id: 'j1', status: 'failed' }));
    component.jobId = 'j1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component['sub']).toBeNull();
  });

  it('handles polling error', async () => {
    vi.useFakeTimers();
    api.getStatus.mockReturnValue(throwError(() => new Error('x')));
    component.jobId = 'j1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component.loading).toBe(false);
    expect(component['sub']).toBeNull();
  });

  it('ngOnDestroy unsubscribes', async () => {
    vi.useFakeTimers();
    api.getStatus.mockReturnValue(of({ job_id: 'j1', status: 'running' }));
    component.jobId = 'j1';
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(1);
    expect(component['sub']).toBeTruthy();
    component.ngOnDestroy();
  });
});
