import { ComponentFixture, TestBed } from '@angular/core/testing';
import { MatDialog } from '@angular/material/dialog';
import { MatSnackBar } from '@angular/material/snack-bar';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { JobScanPanelComponent } from './job-scan-panel.component';
import type { JobMatchResponse, JobMatchRunDetail, RankedJob } from '../../models';

function makeRankedJob(): RankedJob {
  return {
    posting: {
      title: 'Engineer',
      company: 'Acme',
      location: 'NYC',
      remote_mode: 'remote',
      currency: 'USD',
      url: '',
      source: '',
      description: '',
      fingerprint: 'fp1',
    },
    score: 0.8,
    sub_scores: {
      title_fit: 0.8,
      seniority_fit: 0.8,
      location_fit: 0.8,
      comp_fit: 0.8,
      company_fit: 0.8,
      skills_fit: 0.8,
    },
    recommendation: 'apply',
    rationale: 'Fit.',
    concerns: [],
  };
}

describe('JobScanPanelComponent', () => {
  let fixture: ComponentFixture<JobScanPanelComponent>;
  let component: JobScanPanelComponent;
  let apiSpy: {
    startScan: ReturnType<typeof vi.fn>;
    pollScan: ReturnType<typeof vi.fn>;
    listScanJobs: ReturnType<typeof vi.fn>;
    listRuns: ReturnType<typeof vi.fn>;
    getRun: ReturnType<typeof vi.fn>;
    cancelScanJob: ReturnType<typeof vi.fn>;
    deleteScanJob: ReturnType<typeof vi.fn>;
  };
  let snackSpy: { open: ReturnType<typeof vi.fn> };
  let dialogSpy: { open: ReturnType<typeof vi.fn> };

  async function setup(): Promise<void> {
    apiSpy = {
      startScan: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'pending' })),
      pollScan: vi.fn(),
      listScanJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      listRuns: vi
        .fn()
        .mockReturnValue(
          of([{ run_id: 'r1', status: 'completed', total_found: 5, total_ranked: 3 }])
        ),
      getRun: vi.fn(),
      cancelScanJob: vi.fn(),
      deleteScanJob: vi.fn(),
    };
    snackSpy = { open: vi.fn() };
    dialogSpy = { open: vi.fn() };
    await TestBed.configureTestingModule({
      imports: [JobScanPanelComponent],
      providers: [
        provideNoopAnimations(),
        { provide: JobMatchingApiService, useValue: apiSpy },
        { provide: MatSnackBar, useValue: snackSpy },
        { provide: MatDialog, useValue: dialogSpy },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(JobScanPanelComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }

  it('loads scan jobs and runs on init', async () => {
    await setup();
    expect(apiSpy.listScanJobs).toHaveBeenCalled();
    expect(apiSpy.listRuns).toHaveBeenCalled();
    expect(component.runs.length).toBe(1);
  });

  it('validates the scan option bounds', async () => {
    await setup();
    component.form.patchValue({ max_queries: 30 });
    expect(component.form.invalid).toBe(true);
    component.form.patchValue({ max_queries: 6, top_n: 0 });
    expect(component.form.invalid).toBe(true);
    component.form.patchValue({ top_n: 15 });
    expect(component.form.valid).toBe(true);
  });

  it('maps only non-empty overrides into profile_overrides', async () => {
    await setup();
    expect(component.toRequest().profile_overrides).toBeNull();

    component.form.patchValue({
      override_titles: 'EM, Staff Eng',
      override_keywords: '  ',
      override_locations: 'Denver',
    });
    const request = component.toRequest();
    expect(request.profile_overrides).toEqual({
      target_titles: ['EM', 'Staff Eng'],
      locations: ['Denver'],
    });
    expect(request.max_queries).toBe(6);
    expect(request.exclude_seen).toBe(true);
  });

  it('runs a scan, refreshes jobs after submission, announces completion, and emits scanCompleted', async () => {
    await setup();
    const result: JobMatchResponse = {
      run_id: 'r2',
      ranked_jobs: [],
      total_found: 12,
      total_ranked: 8,
      profile_snapshot: {} as never,
      generated_at: '',
    };
    apiSpy.pollScan.mockReturnValue(of(result));
    const completed = vi.fn();
    component.scanCompleted.subscribe(completed);
    apiSpy.listScanJobs.mockClear();

    component.startScan();

    expect(apiSpy.startScan).toHaveBeenCalled();
    // The jobs list refresh is sequenced after the POST response, so the
    // pending row (and its Cancel button) is guaranteed to exist server-side.
    expect(apiSpy.listScanJobs).toHaveBeenCalled();
    expect(apiSpy.pollScan).toHaveBeenCalledWith('j1');
    expect(component.scanning).toBe(false);
    expect(completed).toHaveBeenCalled();
    expect(snackSpy.open).toHaveBeenCalledWith(
      expect.stringContaining('8 roles ranked from 12 found'),
      'Dismiss',
      expect.anything()
    );
  });

  it('surfaces a scan failure', async () => {
    await setup();
    apiSpy.pollScan.mockReturnValue(throwError(() => new Error('scan failed hard')));
    component.startScan();
    expect(component.scanning).toBe(false);
    expect(component.scanError).toBe('scan failed hard');
  });

  it('does not start when the form is invalid or a scan is running', async () => {
    await setup();
    component.form.patchValue({ max_queries: 99 });
    component.startScan();
    expect(apiSpy.startScan).not.toHaveBeenCalled();

    component.form.patchValue({ max_queries: 6 });
    component.scanning = true;
    component.startScan();
    expect(apiSpy.startScan).not.toHaveBeenCalled();
  });

  it('cancels an active scan job', async () => {
    await setup();
    apiSpy.cancelScanJob.mockReturnValue(of({ job_id: 'j1', success: true }));
    component.cancelJob({ job_id: 'j1', status: 'running' });
    expect(apiSpy.cancelScanJob).toHaveBeenCalledWith('j1');
    expect(snackSpy.open).toHaveBeenCalledWith('Scan cancelled.', 'Dismiss', expect.anything());
  });

  it('deletes a scan job only after confirmation', async () => {
    await setup();
    apiSpy.deleteScanJob.mockReturnValue(of({ job_id: 'j1', deleted: true }));

    dialogSpy.open.mockReturnValue({ afterClosed: () => of(false) });
    component.deleteJob({ job_id: 'j1', status: 'completed' });
    expect(apiSpy.deleteScanJob).not.toHaveBeenCalled();

    dialogSpy.open.mockReturnValue({ afterClosed: () => of(true) });
    component.deleteJob({ job_id: 'j1', status: 'completed' });
    expect(apiSpy.deleteScanJob).toHaveBeenCalledWith('j1');
  });

  it('expands a run by loading its detail and collapses on second toggle', async () => {
    await setup();
    const detail: JobMatchRunDetail = {
      run_id: 'r1',
      status: 'completed',
      total_found: 5,
      total_ranked: 1,
      ranked_jobs: [makeRankedJob()],
    };
    apiSpy.getRun.mockReturnValue(of(detail));

    component.toggleRun(component.runs[0]);
    expect(apiSpy.getRun).toHaveBeenCalledWith('r1');
    expect(component.runDetail('r1')).toEqual(detail);
    expect(component.isRunLoading('r1')).toBe(false);

    component.toggleRun(component.runs[0]);
    expect(component.runDetail('r1')).toBeNull();
  });

  it('adapts a ranked job to a read-only listing', async () => {
    await setup();
    const listing = component.asListing(makeRankedJob(), 'r1');
    expect(listing.fingerprint).toBe('fp1');
    expect(listing.run_id).toBe('r1');
    expect(listing.status).toBe('new');
  });
});
