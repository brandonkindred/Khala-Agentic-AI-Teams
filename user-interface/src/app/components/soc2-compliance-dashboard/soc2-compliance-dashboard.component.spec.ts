import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { Soc2ComplianceApiService } from '../../services/soc2-compliance-api.service';
import { Soc2ComplianceDashboardComponent } from './soc2-compliance-dashboard.component';

describe('Soc2ComplianceDashboardComponent', () => {
  let component: Soc2ComplianceDashboardComponent;
  let fixture: ComponentFixture<Soc2ComplianceDashboardComponent>;
  let apiSpy: { runAudit: ReturnType<typeof vi.fn>; health: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = {
      runAudit: vi.fn(),
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
    };
    await TestBed.configureTestingModule({
      imports: [Soc2ComplianceDashboardComponent],
      providers: [{ provide: Soc2ComplianceApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(Soc2ComplianceDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('onSubmit should call api.runAudit and set jobId on success', () => {
    apiSpy.runAudit.mockReturnValue(of({ job_id: 'job-1' }));
    component.onSubmit({ scope: 'test' } as any);
    expect(apiSpy.runAudit).toHaveBeenCalledWith({ scope: 'test' });
    expect(component.jobId).toBe('job-1');
    expect(component.loading).toBe(false);
  });

  it('onSubmit should set error on failure', () => {
    apiSpy.runAudit.mockReturnValue(throwError(() => ({ error: { detail: 'Failed' } })));
    component.onSubmit({ scope: 'x' } as any);
    expect(component.error).toBeTruthy();
    expect(component.loading).toBe(false);
  });

  it('healthCheck should call api.health', () => {
    component.healthCheck().subscribe((r) => expect(r).toEqual({ status: 'ok' }));
    expect(apiSpy.health).toHaveBeenCalled();
  });
});
