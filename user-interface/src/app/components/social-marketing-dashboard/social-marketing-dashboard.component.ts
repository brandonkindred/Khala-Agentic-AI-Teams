import { Component, inject, OnDestroy, OnInit } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { Subscription } from 'rxjs';
import { MatTabsModule } from '@angular/material/tabs';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { SocialMarketingApiService } from '../../services/social-marketing-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { SocialMarketingRunFormComponent } from '../social-marketing-run-form/social-marketing-run-form.component';
import { SocialMarketingStatusComponent } from '../social-marketing-status/social-marketing-status.component';
import { SocialMarketingPerformanceComponent } from '../social-marketing-performance/social-marketing-performance.component';
import { SocialMarketingReviseComponent } from '../social-marketing-revise/social-marketing-revise.component';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import type {
  RunMarketingTeamRequest,
  PostPerformanceObservation,
  ReviseMarketingTeamRequest,
} from '../../models';

@Component({
  selector: 'app-social-marketing-dashboard',
  standalone: true,
  imports: [
    MatTabsModule,
    MatButtonModule,
    MatIconModule,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    HealthIndicatorComponent,
    SocialMarketingRunFormComponent,
    SocialMarketingStatusComponent,
    SocialMarketingPerformanceComponent,
    SocialMarketingReviseComponent,
    TeamAssistantChatComponent,
  ],
  templateUrl: './social-marketing-dashboard.component.html',
  styleUrl: './social-marketing-dashboard.component.scss',
})
export class SocialMarketingDashboardComponent implements OnInit, OnDestroy {
  private readonly api = inject(SocialMarketingApiService);
  private readonly route = inject(ActivatedRoute);
  private queryParamsSub: Subscription | null = null;

  mode: 'chat' | 'form' = 'chat';
  loading = false;
  error: string | null = null;
  jobId: string | null = null;

  healthCheck = (): ReturnType<SocialMarketingApiService['health']> =>
    this.api.health();

  ngOnInit(): void {
    this.queryParamsSub = this.route.queryParams.subscribe((params) => {
      const id = params['jobId'];
      this.jobId = id ?? null;
    });
  }

  ngOnDestroy(): void {
    this.queryParamsSub?.unsubscribe();
  }

  onRunSubmit(request: RunMarketingTeamRequest): void {
    this.loading = true;
    this.error = null;
    this.jobId = null;
    this.api.run(request).subscribe({
      next: (res) => {
        this.jobId = res.job_id;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Request failed';
        this.loading = false;
      },
    });
  }

  onPerformanceSubmit(observations: PostPerformanceObservation[]): void {
    if (!this.jobId) return;
    this.error = null;
    this.api.ingestPerformance(this.jobId, { observations }).subscribe({
      // eslint-disable-next-line @typescript-eslint/no-empty-function
      next: () => {},
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Ingest failed';
      },
    });
  }

  onReviseSubmit(request: ReviseMarketingTeamRequest): void {
    if (!this.jobId) return;
    this.loading = true;
    this.error = null;
    this.api.revise(this.jobId, request).subscribe({
      next: () => {
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Revise failed';
        this.loading = false;
      },
    });
  }

  stopJob(): void {
    if (!this.jobId) return;
    this.error = null;
    this.api.cancelJob(this.jobId).subscribe({
      next: () => {
        this.jobId = null;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to cancel job';
      },
    });
  }

  deleteJob(): void {
    if (!this.jobId) return;
    if (!confirm('Delete this job? This cannot be undone.')) return;
    const id = this.jobId;
    this.error = null;
    this.api.deleteJob(id).subscribe({
      next: () => {
        this.jobId = null;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to delete job';
      },
    });
  }

  clearJob(): void {
    this.jobId = null;
    this.error = null;
  }
}
