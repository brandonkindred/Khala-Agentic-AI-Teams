import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { ActivatedRoute, Router, provideRouter } from '@angular/router';
import { of, Subject } from 'rxjs';
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
  let navigateSpy: ReturnType<typeof vi.spyOn>;
  let queryParams$: Subject<Record<string, string>>;

  async function setup(initialParams: Record<string, string> = {}): Promise<void> {
    apiSpy = {
      health: vi.fn().mockReturnValue(of({ status: 'ok' })),
      listListings: vi.fn().mockReturnValue(of({ listings: [], total: 0, counts: {} })),
      listScanJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      listRuns: vi.fn().mockReturnValue(of([])),
      getProfile: vi.fn().mockReturnValue(of({})),
    };
    queryParams$ = new Subject<Record<string, string>>();
    await TestBed.configureTestingModule({
      imports: [JobMatchingDashboardComponent],
      providers: [
        provideNoopAnimations(),
        // Real router (child RouterLinks need it); ActivatedRoute stubbed so
        // the spec can drive ?tab= values.
        provideRouter([]),
        { provide: JobMatchingApiService, useValue: apiSpy },
        { provide: ActivatedRoute, useValue: { queryParams: queryParams$.asObservable() } },
      ],
    }).compileComponents();
    navigateSpy = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    fixture = TestBed.createComponent(JobMatchingDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    queryParams$.next(initialParams);
    fixture.detectChanges();
  }

  it('should create with the three tabs', async () => {
    await setup();
    expect(component).toBeTruthy();
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('Job Matching');
    expect(text).toContain('Listings');
    expect(text).toContain('Scan & Runs');
    expect(text).toContain('Profile');
  });

  it('healthCheck delegates to api.health', async () => {
    await setup();
    component.healthCheck().subscribe((r) => expect(r).toEqual({ status: 'ok' }));
    expect(apiSpy.health).toHaveBeenCalled();
  });

  it('activates the tab named in the ?tab= query param', async () => {
    await setup({ tab: 'profile' });
    expect(component.selectedTabIndex).toBe(2);
    queryParams$.next({ tab: 'scans' });
    expect(component.selectedTabIndex).toBe(1);
    // Unknown values leave the selection unchanged.
    queryParams$.next({ tab: 'bogus' });
    expect(component.selectedTabIndex).toBe(1);
  });

  it('writes the active tab back into the URL without polluting history', async () => {
    await setup();
    component.onTabIndexChange(2);
    expect(component.selectedTabIndex).toBe(2);
    expect(navigateSpy).toHaveBeenCalledWith(
      [],
      expect.objectContaining({
        queryParams: { tab: 'profile' },
        queryParamsHandling: 'merge',
        replaceUrl: true,
      })
    );
  });

  it('switches to the Scan tab and focuses it when the empty state asks to start a scan', async () => {
    vi.useFakeTimers();
    await setup();
    const focusStart = vi.fn();
    component.scanPanel = { focusStart } as never;
    component.onStartScanRequested();
    expect(component.selectedTabIndex).toBe(1);
    expect(navigateSpy).toHaveBeenCalledWith(
      [],
      expect.objectContaining({ queryParams: { tab: 'scans' } })
    );
    // Focus is handed off after the tab body renders (deferred).
    vi.runAllTimers();
    expect(focusStart).toHaveBeenCalled();
    vi.useRealTimers();
  });

  it('switches to the Profile tab and focuses it when asked to set up the profile', async () => {
    vi.useFakeTimers();
    await setup();
    const focus = vi.fn();
    component.profileForm = { focus } as never;
    component.onSetupProfileRequested();
    expect(component.selectedTabIndex).toBe(2);
    expect(navigateSpy).toHaveBeenCalledWith(
      [],
      expect.objectContaining({ queryParams: { tab: 'profile' } })
    );
    vi.runAllTimers();
    expect(focus).toHaveBeenCalled();
    vi.useRealTimers();
  });

  it('onScanCompleted reloads the listings panel when present', async () => {
    await setup();
    const load = vi.fn();
    component.listingsPanel = { load } as never;
    component.onScanCompleted();
    expect(load).toHaveBeenCalled();
    // No panel (tab not yet rendered) is a safe no-op.
    component.listingsPanel = undefined;
    expect(() => component.onScanCompleted()).not.toThrow();
  });
});
