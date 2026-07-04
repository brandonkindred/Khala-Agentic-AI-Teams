import { TestBed } from '@angular/core/testing';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { JobScanPanelComponent } from './job-scan-panel.component';
import { expectNoAxeViolations } from '../../testing/a11y';

describe('JobScanPanelComponent a11y', () => {
  async function createFixture(runs: unknown[], jobs: unknown[] = []) {
    const apiSpy = {
      startScan: vi.fn(),
      pollScan: vi.fn(),
      listScanJobs: vi.fn().mockReturnValue(of({ jobs })),
      listRuns: vi.fn().mockReturnValue(of(runs)),
      getRun: vi.fn(),
      cancelScanJob: vi.fn(),
      deleteScanJob: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [JobScanPanelComponent],
      providers: [
        provideNoopAnimations(),
        provideRouter([]),
        { provide: JobMatchingApiService, useValue: apiSpy },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(JobScanPanelComponent);
    fixture.detectChanges();
    return fixture;
  }

  it('has no axe violations with the form, jobs, and run history rendered', async () => {
    const fixture = await createFixture(
      [{ run_id: 'r1', status: 'completed', total_found: 5, total_ranked: 3, completed_at: '2026-07-01' }],
      [{ job_id: 'j1', status: 'running' }]
    );
    // Guards: form fields, a scan-job row, and a run row are all audited.
    expect(fixture.nativeElement.querySelector('form')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.scan-job-row')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.runs-table tbody tr')).toBeTruthy();
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('has no axe violations with no runs yet', async () => {
    const fixture = await createFixture([]);
    expect(fixture.nativeElement.textContent).toContain('No runs yet');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);
});
