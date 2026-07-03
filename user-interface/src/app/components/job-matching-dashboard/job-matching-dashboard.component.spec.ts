import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { JobMatchingDashboardComponent } from './job-matching-dashboard.component';

describe('JobMatchingDashboardComponent', () => {
  let fixture: ComponentFixture<JobMatchingDashboardComponent>;
  let component: JobMatchingDashboardComponent;
  let apiSpy: {
    health: ReturnType<typeof vi.fn>;
    listListings: ReturnType<typeof vi.fn>;
    listScanJobs: ReturnType<typeof vi.fn>;
    listRuns: ReturnType<typeof vi.fn>;
    getProfile: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      listListings: vi.fn().mockReturnValue(of({ listings: [], total: 0, counts: {} })),
      listScanJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      listRuns: vi.fn().mockReturnValue(of([])),
      getProfile: vi.fn().mockReturnValue(of({})),
    };
    await TestBed.configureTestingModule({
      imports: [JobMatchingDashboardComponent],
      providers: [
        provideNoopAnimations(),
        provideRouter([]),
        { provide: JobMatchingApiService, useValue: apiSpy },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(JobMatchingDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create with the three tabs', () => {
    expect(component).toBeTruthy();
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('Job Matching');
    expect(text).toContain('Listings');
    expect(text).toContain('Scan & Runs');
    expect(text).toContain('Profile');
  });

  it('healthCheck delegates to api.health', () => {
    component.healthCheck().subscribe((r) => expect(r).toEqual({ status: 'ok' }));
    expect(apiSpy.health).toHaveBeenCalled();
  });

  it('onScanCompleted reloads the listings panel when present', () => {
    const load = vi.fn();
    component.listingsPanel = { load } as never;
    component.onScanCompleted();
    expect(load).toHaveBeenCalled();
    // No panel (tab not yet rendered) is a safe no-op.
    component.listingsPanel = undefined;
    expect(() => component.onScanCompleted()).not.toThrow();
  });
});
