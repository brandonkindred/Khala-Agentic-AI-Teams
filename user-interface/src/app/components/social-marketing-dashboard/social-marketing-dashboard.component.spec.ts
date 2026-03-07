import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { SocialMarketingApiService } from '../../services/social-marketing-api.service';
import { SocialMarketingDashboardComponent } from './social-marketing-dashboard.component';

describe('SocialMarketingDashboardComponent', () => {
  let component: SocialMarketingDashboardComponent;
  let fixture: ComponentFixture<SocialMarketingDashboardComponent>;
  let apiSpy: {
    run: ReturnType<typeof vi.fn>;
    health: ReturnType<typeof vi.fn>;
    ingestPerformance: ReturnType<typeof vi.fn>;
    revise: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      run: vi.fn(),
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      ingestPerformance: vi.fn().mockReturnValue(of({})),
      revise: vi.fn().mockReturnValue(of({})),
    };
    await TestBed.configureTestingModule({
      imports: [SocialMarketingDashboardComponent, NoopAnimationsModule],
      providers: [{ provide: SocialMarketingApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(SocialMarketingDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('onRunSubmit should call api.run and set jobId on success', () => {
    apiSpy.run.mockReturnValue(of({ job_id: 'job-1' }));
    component.onRunSubmit({ brief: 'test' } as any);
    expect(apiSpy.run).toHaveBeenCalledWith({ brief: 'test' });
    expect(component.jobId).toBe('job-1');
    expect(component.loading).toBe(false);
  });

  it('onRunSubmit should set error on failure', () => {
    apiSpy.run.mockReturnValue(throwError(() => ({ error: { detail: 'Failed' } })));
    component.onRunSubmit({ brief: 'x' } as any);
    expect(component.error).toBeTruthy();
  });

  it('onPerformanceSubmit should call api.ingestPerformance when jobId is set', () => {
    component.jobId = 'job-1';
    component.onPerformanceSubmit([{ metric: 'clicks', value: 10 }] as any);
    expect(apiSpy.ingestPerformance).toHaveBeenCalledWith('job-1', expect.any(Object));
  });

  it('onReviseSubmit should call api.revise when jobId is set', () => {
    component.jobId = 'job-1';
    component.onReviseSubmit({ feedback: 'revise' } as any);
    expect(apiSpy.revise).toHaveBeenCalledWith('job-1', { feedback: 'revise' });
  });

  it('healthCheck should call api.health', () => {
    component.healthCheck().subscribe((r) => expect(r).toEqual({ status: 'ok' }));
    expect(apiSpy.health).toHaveBeenCalled();
  });
});
