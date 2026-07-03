import { TestBed } from '@angular/core/testing';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { axe } from 'vitest-axe';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { JobScanPanelComponent } from './job-scan-panel.component';

// `color-contrast` is disabled because jsdom can't paint; contrast is
// enforced by the --kh-* token system + the SCSS contrast guard spec.
const axeOptions = { rules: { 'color-contrast': { enabled: false } } };

describe('JobScanPanelComponent a11y', () => {
  async function createFixture(runs: unknown[], jobs: unknown[] = []) {
    const apiSpy = {
      runScan: vi.fn(),
      listScanJobs: vi.fn().mockReturnValue(of({ jobs })),
      listRuns: vi.fn().mockReturnValue(of(runs)),
      getRun: vi.fn(),
      cancelScanJob: vi.fn(),
      deleteScanJob: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [JobScanPanelComponent],
      providers: [provideNoopAnimations(), { provide: JobMatchingApiService, useValue: apiSpy }],
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
    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  }, 15000);

  it('has no axe violations with no runs yet', async () => {
    const fixture = await createFixture([]);
    expect(fixture.nativeElement.textContent).toContain('No runs yet');
    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  }, 15000);
});
