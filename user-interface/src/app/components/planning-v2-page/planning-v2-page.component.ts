import { Component, inject, OnDestroy, OnInit } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { PlanningV2RunFormComponent } from '../planning-v2-run-form/planning-v2-run-form.component';
import { PlanningV2JobStatusComponent } from '../planning-v2-job-status/planning-v2-job-status.component';
import { PlanningArtifactsViewerComponent } from '../planning-artifacts-viewer/planning-artifacts-viewer.component';
import type { PlanningV2RunRequest, RunningJobSummary } from '../../models';

@Component({
  selector: 'app-planning-v2-page',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    HealthIndicatorComponent,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    PlanningV2RunFormComponent,
    PlanningV2JobStatusComponent,
    PlanningArtifactsViewerComponent,
  ],
  templateUrl: './planning-v2-page.component.html',
  styleUrl: './planning-v2-page.component.scss',
})
export class PlanningV2PageComponent implements OnInit, OnDestroy {
  private readonly api = inject(SoftwareEngineeringApiService);
  private readonly route = inject(ActivatedRoute);
  private jobsSub: Subscription | null = null;
  private pendingJobId: string | null = null;

  loading = false;
  error: string | null = null;
  jobId: string | null = null;
  planningV2Jobs: RunningJobSummary[] = [];
  selectedJob: RunningJobSummary | null = null;

  healthCheck = (): ReturnType<SoftwareEngineeringApiService['health']> =>
    this.api.health();

  ngOnInit(): void {
    this.route.queryParams.subscribe((params) => {
      const jobId = params['jobId'];
      if (jobId) {
        this.pendingJobId = jobId;
      }
    });

    this.jobsSub = timer(0, 15000).pipe(
      switchMap(() => this.api.getPlanningV2Jobs())
    ).subscribe({
      next: (res) => {
        this.planningV2Jobs = res.jobs;
        if (this.planningV2Jobs.length === 0) {
          this.selectedJob = null;
        } else if (this.selectedJob && !this.planningV2Jobs.find(j => j.job_id === this.selectedJob!.job_id)) {
          this.selectedJob = null;
        }

        if (this.pendingJobId) {
          const job = this.planningV2Jobs.find(j => j.job_id === this.pendingJobId);
          if (job) {
            this.selectJob(job);
          } else {
            this.jobId = this.pendingJobId;
          }
          this.pendingJobId = null;
        } else if (this.planningV2Jobs.length > 0 && !this.selectedJob && !this.jobId) {
          this.selectJob(this.planningV2Jobs[0]);
        }
      },
    });
  }

  ngOnDestroy(): void {
    this.jobsSub?.unsubscribe();
  }

  selectJob(job: RunningJobSummary): void {
    this.selectedJob = job;
    this.jobId = job.job_id;
  }

  onPlanningV2Submit(request: PlanningV2RunRequest): void {
    this.loading = true;
    this.error = null;
    this.jobId = null;
    this.selectedJob = null;
    this.api.runPlanningV2(request).subscribe({
      next: (res) => {
        this.jobId = res.job_id;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Planning-V2 request failed';
        this.loading = false;
      },
    });
  }
}
