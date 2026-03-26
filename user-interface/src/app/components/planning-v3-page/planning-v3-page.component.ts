import { Component, inject, OnDestroy, OnInit } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { Subscription, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { PlanningV3ApiService } from '../../services/planning-v3-api.service';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { PlanningV3RunFormComponent } from '../planning-v3-run-form/planning-v3-run-form.component';
import { PlanningV3JobStatusComponent } from '../planning-v3-job-status/planning-v3-job-status.component';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import type { PlanningV3RunRequest, PlanningV3JobSummary } from '../../models';

@Component({
  selector: 'app-planning-v3-page',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatIconModule,
    MatButtonModule,
    HealthIndicatorComponent,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    PlanningV3RunFormComponent,
    PlanningV3JobStatusComponent,
    TeamAssistantChatComponent,
  ],
  templateUrl: './planning-v3-page.component.html',
  styleUrl: './planning-v3-page.component.scss',
})
export class PlanningV3PageComponent implements OnInit, OnDestroy {
  private readonly api = inject(PlanningV3ApiService);
  private readonly route = inject(ActivatedRoute);
  private jobsSub: Subscription | null = null;
  private pendingJobId: string | null = null;

  mode: 'chat' | 'form' = 'chat';
  loading = false;
  error: string | null = null;
  jobId: string | null = null;
  planningV3Jobs: PlanningV3JobSummary[] = [];
  selectedJob: PlanningV3JobSummary | null = null;

  healthCheck = (): ReturnType<PlanningV3ApiService['health']> =>
    this.api.health();

  ngOnInit(): void {
    this.route.queryParams.subscribe((params) => {
      const jobId = params['jobId'];
      if (jobId) {
        this.pendingJobId = jobId;
      }
    });

    this.jobsSub = timer(0, 15000).pipe(
      switchMap(() => this.api.getJobs())
    ).subscribe({
      next: (res) => {
        this.planningV3Jobs = res.jobs ?? [];
        if (this.planningV3Jobs.length === 0) {
          this.selectedJob = null;
        } else if (this.selectedJob && !this.planningV3Jobs.find(j => j.job_id === this.selectedJob!.job_id)) {
          this.selectedJob = null;
        }

        if (this.pendingJobId) {
          const job = this.planningV3Jobs.find(j => j.job_id === this.pendingJobId);
          if (job) {
            this.selectJob(job);
          } else {
            this.jobId = this.pendingJobId;
          }
          this.pendingJobId = null;
        } else if (this.planningV3Jobs.length > 0 && !this.selectedJob && !this.jobId) {
          this.selectJob(this.planningV3Jobs[0]);
        }
      },
    });
  }

  ngOnDestroy(): void {
    this.jobsSub?.unsubscribe();
  }

  selectJob(job: PlanningV3JobSummary): void {
    this.selectedJob = job;
    this.jobId = job.job_id;
  }

  onPlanningV3Submit(request: PlanningV3RunRequest): void {
    this.loading = true;
    this.error = null;
    this.jobId = null;
    this.selectedJob = null;
    this.api.run(request).subscribe({
      next: (res) => {
        this.jobId = res.job_id;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Planning V3 request failed';
        this.loading = false;
      },
    });
  }
}
