import {
  Component,
  DestroyRef,
  ElementRef,
  EventEmitter,
  OnInit,
  Output,
  inject,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { switchMap, tap } from 'rxjs/operators';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { DatePipe } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatDialog } from '@angular/material/dialog';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSnackBar } from '@angular/material/snack-bar';
import {
  ConfirmDialogComponent,
  type ConfirmDialogData,
} from '../../shared/confirm-dialog/confirm-dialog.component';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import type {
  JobMatchRequest,
  JobMatchRunDetail,
  JobMatchRunSummary,
  JobSeekerProfile,
  ScanJobListItem,
} from '../../models';

/** Split a comma-separated override input into trimmed non-empty values. */
function splitCsv(value: string): string[] {
  return value
    .split(',')
    .map((v) => v.trim())
    .filter((v) => v.length > 0);
}

/**
 * Scan launcher + run history. Submits a scan with per-run options (and
 * optional profile overrides), polls it to completion, lists active scan
 * jobs with cancel/delete, and shows past runs whose ranked roles expand
 * inline as read-only listing cards.
 */
@Component({
  selector: 'app-job-scan-panel',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    RouterLink,
    DatePipe,
    DecimalPipe,
    MatButtonModule,
    MatCardModule,
    MatCheckboxModule,
    MatExpansionModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatProgressBarModule,
  ],
  templateUrl: './job-scan-panel.component.html',
  styleUrl: './job-scan-panel.component.scss',
})
export class JobScanPanelComponent implements OnInit {
  /** Emitted when a scan completes so the dashboard can refresh Listings. */
  @Output() scanCompleted = new EventEmitter<void>();

  private readonly api = inject(JobMatchingApiService);
  private readonly fb = inject(FormBuilder);
  private readonly snackBar = inject(MatSnackBar);
  private readonly dialog = inject(MatDialog);
  private readonly destroyRef = inject(DestroyRef);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  scanning = false;
  scanError: string | null = null;
  /** Polite status text for screen readers as a scan starts/finishes. */
  scanStatus = '';
  scanJobs: ScanJobListItem[] = [];
  runs: JobMatchRunSummary[] = [];
  runsLoading = false;
  /** run_id -> loaded detail; presence means the row is expanded. */
  expandedRuns = new Map<string, JobMatchRunDetail | 'loading'>();

  /**
   * Monotonic token per jobs-list refresh. Refreshes fire from several sites
   * (submit, completion, cancel, delete); without ordering, a slow early GET
   * arriving after a later one would revert a terminal job to "running".
   */
  private scanJobsSeq = 0;

  readonly form = this.fb.nonNullable.group({
    max_queries: [6, [Validators.required, Validators.min(1), Validators.max(25)]],
    max_roles: [40, [Validators.required, Validators.min(1), Validators.max(200)]],
    top_n: [15, [Validators.required, Validators.min(1), Validators.max(200)]],
    exclude_seen: [true],
    override_titles: [''],
    override_locations: [''],
    override_keywords: [''],
  });

  ngOnInit(): void {
    this.refreshScanJobs();
    this.refreshRuns();
  }

  /** Assemble the scan request, mapping non-empty overrides into profile_overrides. */
  toRequest(): JobMatchRequest {
    const raw = this.form.getRawValue();
    const overrides: Partial<JobSeekerProfile> = {};
    const titles = splitCsv(raw.override_titles);
    const locations = splitCsv(raw.override_locations);
    const keywords = splitCsv(raw.override_keywords);
    if (titles.length) {
      overrides.target_titles = titles;
    }
    if (locations.length) {
      overrides.locations = locations;
    }
    if (keywords.length) {
      overrides.keywords = keywords;
    }
    return {
      max_queries: raw.max_queries,
      max_roles: raw.max_roles,
      top_n: raw.top_n,
      exclude_seen: raw.exclude_seen,
      profile_overrides: Object.keys(overrides).length ? overrides : null,
    };
  }

  startScan(): void {
    if (this.form.invalid || this.scanning) {
      return;
    }
    this.scanning = true;
    this.scanError = null;
    this.scanStatus = 'Scan started — searching for roles. This can take a few minutes.';
    this.api
      .startScan(this.toRequest())
      .pipe(
        // Refresh the jobs list only after the POST response — the backend
        // creates the job row before responding, so the pending row (and its
        // Cancel button) is guaranteed to be visible for the whole scan.
        tap(() => this.refreshScanJobs()),
        switchMap((submission) => this.api.pollScan(submission.job_id)),
        takeUntilDestroyed(this.destroyRef)
      )
      .subscribe({
        next: (result) => {
          this.scanning = false;
          this.scanStatus = `Scan complete — ${result.total_ranked} roles ranked from ${result.total_found} found.`;
          this.snackBar.open(
            `Scan complete — ${result.total_ranked} roles ranked from ${result.total_found} found.`,
            'Dismiss',
            { duration: 5000 }
          );
          this.refreshScanJobs();
          this.refreshRuns();
          this.scanCompleted.emit();
        },
        error: (err) => {
          this.scanning = false;
          this.scanStatus = 'The scan failed.';
          this.scanError = err?.error?.detail ?? err?.message ?? 'The scan failed.';
          this.refreshScanJobs();
        },
      });
  }

  /** Focus the "Start a scan" heading — the dashboard's landing point when it
   *  programmatically switches to this tab (so keyboard focus follows the view). */
  focusStart(): void {
    this.host.nativeElement.querySelector<HTMLElement>('#jm-scan-heading')?.focus();
  }

  /** Human label for a scan job — a short id, not the full opaque UUID. */
  jobLabel(job: ScanJobListItem): string {
    return `scan ${this.shortRunId(job.job_id)}`;
  }

  /** First segment of a UUID — enough to disambiguate rows without the full id. */
  shortRunId(id: string): string {
    return id ? id.split('-')[0] : id;
  }

  refreshScanJobs(): void {
    const seq = ++this.scanJobsSeq;
    this.api
      .listScanJobs()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (res) => {
          if (seq === this.scanJobsSeq) {
            this.scanJobs = res.jobs;
          }
        },
        error: () => undefined, // the jobs list is auxiliary; scan errors surface elsewhere
      });
  }

  refreshRuns(): void {
    this.runsLoading = true;
    this.api
      .listRuns()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (runs) => {
          this.runs = runs;
          this.runsLoading = false;
        },
        error: () => {
          this.runsLoading = false;
        },
      });
  }

  isActive(job: ScanJobListItem): boolean {
    return job.status === 'pending' || job.status === 'running';
  }

  cancelJob(job: ScanJobListItem): void {
    this.api
      .cancelScanJob(job.job_id)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: () => {
          this.snackBar.open('Scan cancelled.', 'Dismiss', { duration: 3000 });
          this.refreshScanJobs();
          // Cancel replaces this row's button with a Delete button; keep the
          // keyboard user anchored on the same row rather than dropping to body.
          this.restoreJobFocus(job.job_id);
        },
        error: () => this.snackBar.open('Could not cancel the scan.', 'Dismiss', { duration: 4000 }),
      });
  }

  /**
   * After a scan-job row re-renders, keep keyboard focus anchored: focus the
   * named row's action button, or the "Scan jobs" heading when it's gone.
   */
  private restoreJobFocus(jobId: string): void {
    setTimeout(() => {
      const root = this.host.nativeElement;
      const row = root.querySelector<HTMLElement>(`[data-job-id="${jobId}"] button`);
      if (row) {
        row.focus();
        return;
      }
      root.querySelector<HTMLElement>('#jm-scanjobs-heading')?.focus();
    });
  }

  deleteJob(job: ScanJobListItem): void {
    const data: ConfirmDialogData = {
      title: 'Delete scan job?',
      message: `This removes the record of ${this.jobLabel(job)}. Ranked listings are kept.`,
      confirmLabel: 'Delete',
      variant: 'danger',
    };
    this.dialog
      .open(ConfirmDialogComponent, { data })
      .afterClosed()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((confirmed) => {
        if (!confirmed) {
          return;
        }
        this.api
          .deleteScanJob(job.job_id)
          .pipe(takeUntilDestroyed(this.destroyRef))
          .subscribe({
            next: () => {
              this.refreshScanJobs();
              // The row is gone; anchor focus on the section heading (or the
              // confirm dialog already returned focus to a now-removed button).
              this.restoreJobFocus(job.job_id);
            },
            error: () =>
              this.snackBar.open('Could not delete the scan job.', 'Dismiss', { duration: 4000 }),
          });
      });
  }

  toggleRun(run: JobMatchRunSummary): void {
    if (this.expandedRuns.has(run.run_id)) {
      this.expandedRuns.delete(run.run_id);
      return;
    }
    this.expandedRuns.set(run.run_id, 'loading');
    this.api
      .getRun(run.run_id)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (detail) => this.expandedRuns.set(run.run_id, detail),
        error: () => {
          this.expandedRuns.delete(run.run_id);
          this.snackBar.open('Could not load the run detail.', 'Dismiss', { duration: 4000 });
        },
      });
  }

  runDetail(runId: string): JobMatchRunDetail | null {
    const entry = this.expandedRuns.get(runId);
    return entry && entry !== 'loading' ? entry : null;
  }

  isRunLoading(runId: string): boolean {
    return this.expandedRuns.get(runId) === 'loading';
  }
}
