import { TestBed } from '@angular/core/testing';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { JobMatchingDashboardComponent } from './job-matching-dashboard.component';
import { expectNoAxeViolations } from '../../testing/a11y';

describe('JobMatchingDashboardComponent a11y', () => {
  it('has no axe violations with the shell and tabs rendered', async () => {
    const apiSpy = {
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
    const fixture = TestBed.createComponent(JobMatchingDashboardComponent);
    fixture.detectChanges();

    // Guard: the tab bar is actually in the DOM so axe audits it.
    expect(fixture.nativeElement.querySelectorAll('[role="tab"]').length).toBe(3);

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);
});
